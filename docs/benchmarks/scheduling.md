# Schedule an incident over time

Use a schedule when the incident changes over time: a link that goes down and comes back, a failure that starts after the agent begins, or traffic that arrives in bursts. A schedule lives in the task file. It lists the failures, recoveries, and traffic, each at a planned time, and it sets when the agent starts. Each session runs its own timeline after its network and agent infrastructure are ready.

## Run the example

```shell
uv run nika benchmark run --config config/benchmark-schedule.example.yaml
```

This runs the [scheduled benchmark example](../../config/benchmark-schedule.example.yaml) with your configured agent. The example starts an incast traffic profile, brings a client link down twice, introduces a persistent DNS error, and starts diagnosis at 10 seconds.

To check the timeline without a model endpoint, add `--agent mock`. The mock agent reads the ground truth, so use it only for development.

## Write a schedule

Put `schedule` at the root of a task file, next to `scenario`, the deployment options, and an optional `traffic` profile. Declare the failures inside the events. A scheduled task has no task-level `problem`, `problems`, or `inject`.

```yaml
scenario: dc_clos
topo_size: s
schedule:
  agent_start: 0s
  events:
    - problem: link_down
      inject:
        host_name: client_1_1
        intf_name: eth0
      at: 5s
      duration: 10s
      repeat:
        every: 30s
        count: 2
    - problem: link_down
      inject:
        host_name: web_1
        intf_name: eth0
      at: 20s
```

The first event creates two independent occurrences of `link_down` on the link of `client_1_1`. Its planned down windows are 5 to 15 seconds and 35 to 45 seconds. The second event brings down the link of `web_1` at 20 seconds and leaves it down until the session ends.

Each occurrence appears in the logs with its failure, target, planned time, and occurrence number. You do not assign IDs to events; NIKA derives them from their position in the list.

---

The rest of this page is reference material: the event fields, traffic options, how the agent start and scoring work, and the files a run writes.

## Event fields

| Field | Required | Meaning |
| --- | --- | --- |
| `agent_start` | No | Offset at which diagnosis is released. Omitted means wait for the initial events. |
| `events[].at` | No | Planned activation offset. Defaults to `0s`. |
| `events[].problem` | For failures | Registered failure name. |
| `events[].inject` | As the failure requires | Failure parameters. See `nika failure describe <name>`. |
| `events[].traffic` | For traffic | `business`, `background`, or a burst. Cannot be combined with `problem`. |
| `events[].duration` | For bursts and repetition | Planned time from activation to recovery or stop. |
| `events[].repeat.every` | With `repeat` | Interval between planned starts. |
| `events[].repeat.count` | With `repeat` | Total occurrences, including the first. |

## Time values and repetition

- Times are seconds, or strings with `ms`, `s`, `m`, and `h`, such as `1m30s`.
- Offsets are nonnegative. Durations and repetition intervals are positive.
- A repeated event needs a duration no longer than its interval, and a count of at least 1.
- A failure with a `duration` or `repeat` must support recovery. NIKA rejects other timed failures.
- Two failures that overlap in time on the same resource are rejected.
- Infinite repetition is not supported.

## Traffic in a schedule

Define the traffic profile once in the task's `traffic` field, and control when it starts with events. Continuous periodic load belongs in the profile itself. For example, `incast_period_sec: 30` produces periodic fan-in replies without a repeated event. See [background traffic profiles](../operations/network-scenarios.md#background-traffic-profiles).

- `traffic: background` starts the task's `traffic` profile at its offset. It also suppresses the profile's automatic start during setup.
- `traffic: business` starts the environment's business traffic sources at an offset. Their receiving services and maintenance processes still start during setup.
- Without an explicit activation event, each workload starts normally. A persistent workload can be activated once and runs until the session ends.

This event starts the environment's business traffic ten seconds after `t0`:

```yaml
- traffic: business
  at: 10s
```

Use a burst when its timing and count belong to the incident:

```yaml
schedule:
  agent_start: 0s
  events:
    - traffic:
        type: burst
        sources:
          - web_1
        destination: web_2
        protocol: udp
        rate: 100K
        packet_size: 1200
        seed: 42
      at: 15s
      duration: 5s
      repeat:
        every: 30s
        count: 3
```

This fragment uses `dc_clos` endpoints.

- `protocol` is `udp` or `tcp`.
- `rate` is bits per second per source, with decimal `K`, `M`, or `G` suffixes. `100K` means 100,000 bit/s per source.
- `packet_size` is in bytes.
- Receivers are prepared before the timeline starts. Senders get the time left until the planned stop, so a late start does not extend the burst.

## Reuse task files

Keep a reusable task in its own YAML file, with `scenario`, the deployment options, `traffic`, and `schedule` at the root. You can run it directly:

```shell
uv run nika benchmark run --config config/tasks/multi-failure.example.yaml
```

A benchmark file lists complete task files under `cases`. Paths resolve relative to the benchmark file:

```yaml
cases:
  - tasks/multi-failure.example.yaml
  - tasks/healthy.example.yaml
```

Each file defines one complete task. Benchmark fields do not supply defaults to the tasks. A benchmark cannot mix inline tasks with file paths, and it cannot reference another benchmark. Existing matrices that contain only inline tasks still work.

`nika benchmark migrate` expands the references and writes the ground truth into one complete `cases` file. Share that output when you publish a benchmark, so its task definitions travel together.

## Agent start and scoring

Without `agent_start`, NIKA waits for the events at `0s` to finish before it releases diagnosis. Later events keep their planned offsets. Set `agent_start: 0s` to start diagnosis alongside the initial injections. A numeric value releases the agent at that time.

Tasks that use `problem`, `problems`, and `inject` keep working. NIKA runs their failures as persistent events at `0s`, so the source YAML, the task identity, and resume keys do not change. A healthy task has no events and starts diagnosis when the environment is ready. Tasks without a `schedule` use the same scheduler with these defaults.

There is no end time for the agent. When the agent returns a report before the timeline finishes, NIKA keeps that report as provisional. After more events run, NIKA invokes diagnosis again in the same sandbox and gives the agent the previous report. The agent's request budget is shared across these calls. Only the final report is frozen for submission.

The final diagnosis starts after the last scheduled operation. Every finite occurrence must complete before the submission. After that, the agent decides when to stop. The existing request limits and timeouts still apply.

The ground truth includes every distinct root cause that was injected, including failures that later recovered. A scheduler operation failure makes the trial retryable as `infra_failed`. A timeline that does not finish, or a failed timeline, cannot score a submission. A failure that disappears before the agent starts is solvable only with retained diagnostic evidence.

## Timing and cleanup

The session's `t0` begins after deployment, the healthy-network checks, schedule preparation, and agent preparation. Every event offset is measured from that session's clock. Sessions have independent clocks and cancellation, and the benchmark runner controls how many run at once.

- A recovery runs at `at + duration`. If an injection finishes late, the recovery still runs at its original end.
- Commands can run late under host load. NIKA records the acknowledgment and the verification separately.
- Failure operations run one at a time within a session. Traffic operations can run in parallel with them.
- NIKA does not give hard real-time guarantees.

To cancel one running scheduled session, run `nika session close --session_id <id>`. Other sessions keep running. An interrupted benchmark follows its usual cleanup. Resuming starts the timeline again from a clean session.

Cleanup reverses the injection order. If a restoration fails, cleanup records the error and stops restoring older changes before it tears the environment down.

Remote and Firecracker runs execute the same schedule on the lab host, and the agent host waits for the release and completion signals. Firecracker also synchronizes the guest's UTC clock with the host, so host and guest logs line up. Event deadlines still use the session clock.

## Results you can inspect

A scheduled trial directory contains these files:

| File | Contents |
| --- | --- |
| `schedule.json` | The normalized plan and the expected root causes for each event. |
| `schedule-events.jsonl` | One record for each action: planned and actual times, the result, the ground truth, and any cancellation. |
| `schedule-status.json` | Whether the timeline completed, was cancelled, or failed. |
| `ground_truth.json` | The distinct root causes used for scoring. |

The session log `nika.jsonl` also records the schedule milestones. The plan and the ground truth stay on the host, outside the agent's workspace.

Run `uv run nika inspect --result-dir <results>` to see the schedule in the Overview tab, or the events on a shared clock in the Timeline tab. The Timeline shows planned windows as dashed bars and observed windows as solid bars. An occurrence marked `ended` has reached session teardown. A recovery time appears only when the journal records one.

The benchmark terminal shows each session's progress, active occurrences, and next event. With `--output-mode agent`, the same milestones are printed as plain log lines, each prefixed with the task label.

The times in these records are when NIKA sent a command and when it verified the result. The symptom can start before the command finishes. Agents do not report timestamps for scoring.

## Limits

Changing when the agent encounters a failure changes the benchmark, so it requires a new [benchmark revision](../../VERSIONING.md). Published releases do not change.
