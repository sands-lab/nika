# Schedule an incident over time

Task authors can schedule failure injection, recovery, background traffic, and agent startup in one benchmark task. Each session runs its own timeline after the network and agent infrastructure are ready.

Run the [scheduled task example](../../config/benchmark-schedule.example.yaml) with your configured agent:

```shell
uv run nika benchmark run --config config/benchmark-schedule.example.yaml
```

The example starts an incast profile, brings a client link down twice, introduces a persistent DNS error, and starts diagnosis at 10 seconds. To validate the orchestration without a model endpoint, add `--agent mock`. The mock agent uses ground truth and is for development only.

## Run tasks with default timing

Existing tasks with `problem` or `problems` and `inject` continue to work. At runtime, NIKA converts their failures into persistent events at `0s` and runs them through the same scheduler. The agent starts after every initial injection has been verified. Healthy tasks use an empty event list and start diagnosis when the environment and agent infrastructure are ready. The source YAML, task identity, and resume keys stay unchanged.

For authored schedules, `at` defaults to `0s`. Omitting `agent_start` waits for events at `0s` to finish before releasing diagnosis; later events keep their planned offsets. Set `agent_start: 0s` explicitly to start diagnosis alongside the initial injections. A numeric agent offset controls release by time, while the default controls release by injection completion.

## Organize task files

Keep a reusable task in its own YAML with `scenario`, deployment options, `traffic`, and `schedule` at the root. The [multi-failure task](../../config/tasks/multi-failure.example.yaml) is a complete example; run it directly:

```shell
uv run nika benchmark run --config config/tasks/multi-failure.example.yaml
```

A benchmark YAML lists complete task files under `cases`:

```yaml
cases:
  - tasks/multi-failure.example.yaml
  - tasks/healthy.example.yaml
```

Paths resolve relative to the benchmark YAML. Each referenced file defines one complete task, including its scenario, failure events, and traffic settings. Benchmark fields do not supply task defaults. Mixing inline tasks with paths and referencing another benchmark are rejected. The [benchmark example](../../config/benchmark-schedule.example.yaml) references the complete multi-failure task. Existing matrices containing only inline tasks remain supported for compatibility.

`nika benchmark migrate` expands references and materializes ground truth into one complete `cases` YAML. Use that output when sharing or publishing a benchmark so its task definitions travel together.

## Define the task's events

Put `schedule` on the task alongside `scenario`, deployment options, and optional `traffic`. Scheduled tasks declare failures inside their events; omit task-level `problem`, `problems`, and `inject`.

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

These are two independent instances of the same failure. Event descriptions in logs include the failure, target parameters, planned time, and occurrence number. NIKA derives internal instance indices from list positions; authors do not provide IDs.

| Field | Required | Meaning |
| --- | --- | --- |
| `agent_start` | No | Diagnosis release offset; omitted means wait for initial events |
| `events[].at` | No | Planned activation offset, default `0s` |
| `events[].problem` | For failures | Registered failure name |
| `events[].inject` | As required by the failure | Existing failure parameters; see `nika failure describe <name>` |
| `events[].traffic` | For traffic | `business`, `background`, or a burst specification; mutually exclusive with `problem` |
| `events[].duration` | For bursts and repetition | Planned time from activation to recovery or stop |
| `events[].repeat.every` | With `repeat` | Interval between planned starts |
| `events[].repeat.count` | With `repeat` | Total occurrences, including the first |

Times accept seconds as numbers, or strings with `ms`, `s`, `m`, and `h`, including `1m30s`. Offsets are nonnegative; durations and repetition intervals are positive. Repeated events require a duration no longer than their interval and a finite positive integer count.

The first link has planned active windows `[5s, 15s)` and `[35s, 45s)`. The second link remains down from 20 seconds until teardown. A timed failure requires a recovery implementation. Overlapping failures that resolve to the same root-cause resource are rejected.

The scheduler calls `recover_fault` on the instance that injected each occurrence. That instance retains the original state in controller memory and must return `verified: true` after checking restoration. See the [failure recovery contract](../development/creating-benchmark-tasks.md#add-an-injectable-failure). A missing implementation rejects timed scheduling; failed restoration makes the trial `infra_failed`.

## Coordinate traffic

Define the traffic profile once in the task's `traffic` field and control its startup through `schedule.events`. Both fields live in the same task YAML. Keep continuous periodic load in the existing [traffic profile](../operations/network-scenarios.md#background-traffic-profiles). For example, `incast_period_sec: 30` produces periodic fan-in replies alongside the profile's TCP flows. It does not require a repeated scheduler event.

An event with `traffic: background` activates the task's `traffic:` profile at its offset and suppresses its automatic activation during environment startup. Use `traffic: business` to activate the environment's existing business traffic sources at an offset; their receiving services and routine maintenance processes start during environment preparation. Without an explicit activation event, each workload starts normally. Each persistent workload can be activated once and continues until the session ends.

For example, add this event to start the environment's business traffic ten seconds after `t0`:

```yaml
- traffic: business
  at: 10s
```

Use explicit bursts when their timing and count belong to the incident:

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

This fragment uses `dc_clos` endpoints. `protocol` accepts `udp` or `tcp`. Rates are bits per second per source, with decimal `K`, `M`, and `G` suffixes; `100K` means 100,000 bit/s per source. `packet_size` is in bytes. Receivers and resolved flows are prepared before the timeline starts. Senders receive the remaining time to the planned stop boundary, so a late start does not extend the burst. Injection and traffic commands take time; inspect the journal for actual execution times and skew.

## Agent completion and scoring

There is no agent end-time setting. When an agent returns a report before all scheduled operations finish, NIKA retains that provisional report. After event progress, NIKA invokes diagnosis again in the same sandbox, supplies the previous report, and keeps the shared diagnosis request budget. Host-run development agents use the same continuation contract. Only the final report is frozen for submission.

Final diagnosis starts after the last scheduled operation completes. If the timeline finishes while an earlier diagnosis invocation is still running, its report is retained as provisional and one more diagnosis invocation checks the resulting network state.

An event without a duration completes after injection and verification; its fault can remain active. An event with a duration completes after verified recovery. Every finite occurrence must complete before final submission. After that, the agent decides when to finish. Existing agent request limits and agent/trial timeouts still apply.

Ground truth accumulates the distinct root causes that were successfully injected, including faults that later recovered. Each required injection is checked against its materialized per-event ground truth when present. A scheduler operation failure makes the trial retryable `infra_failed`; incomplete or failed timelines cannot score a submission. A failure that disappears before agent startup needs retained diagnostic evidence for the task to be solvable.

## Timing, isolation, and artifacts

The session's `t0` starts after deployment, healthy-network checks, schedule preparation, and agent infrastructure preparation. Events use absolute offsets from that session's monotonic clock. Different sessions have independent origins and cancellation state. The existing benchmark runner controls how many sessions run concurrently.

Use `nika session close --session_id <id>` to cancel one running scheduled session. On Linux, the close command signals the owning trial worker, checks its process identity, and waits for scheduler and environment cleanup. Other sessions keep running. Interrupting a benchmark also follows its existing worker cleanup and abort policy. Resume starts an interrupted timeline from a clean session.

Session cleanup reverses the actual injection order. If restoration fails, cleanup records the error and stops restoring older snapshots before environment teardown.

Recovery is scheduled at `at + duration`. If injection finishes two seconds late, a 30-second window still schedules recovery at its original end. Due commands can execute late under host load; actual command acknowledgment and verification are recorded separately. Failure operations serialize within a session because the existing injection workflow aligns saved configuration artifacts across the lab. Traffic operations can run independently. NIKA does not provide hard real-time execution guarantees.

The trial directory contains:

| Artifact | Contents |
| --- | --- |
| `schedule.json` | Normalized plan and resolved per-event expected root causes |
| `schedule-events.jsonl` | Session and occurrence indices, readable summaries, planned and actual times, operation results, ground truth, and cancellation records |
| `schedule-status.json` | Completion, cancellation, and scheduler failure state |

`ground_truth.json` contains the distinct root causes used for scoring. Each `verified` journal record identifies its event and occurrence, resolved root causes, planned offset, and actual verification time. Join it to the occurrence's `applied` and `recovered` records to reconstruct the incident history. These timestamps record controller acknowledgments and verification results; the network symptom can begin before its command completes. Agents do not report timestamps for scoring.

The session's `nika.jsonl` also contains schedule milestones. Plans and ground truth remain on the host, outside the agent workspace. Task identity includes the ordered event payloads, times, repetitions, traffic, and agent startup offset. Materializing expected root causes preserves that identity. `nika benchmark migrate` supports scheduled tasks for dataset preparation.

Run `uv run nika inspect --result-dir <results>` to view a session's schedule in the Overview or Timeline tab. The schedule lists each occurrence by failure and target, with planned start and stop times, observed application and verification times, and recorded recovery times. It updates while the session runs. An occurrence marked `ended` has reached session teardown; a recovery time appears only when the journal records recovery.

The Timeline tab groups orchestration events under NIKA, alongside Agent and Tools tracks. Zoom in to display scheduled failure and traffic windows on the shared clock. Dashed windows show the plan; solid windows show observed execution. Select an observed window to inspect its log record. The event list shows scheduled failure, traffic, and agent milestones. Scheduler lifecycle and dispatch records remain in the raw logs.

The compact status bar shows the current phase and target, the next operation and countdown, elapsed time, operation progress, and agent startup. NIKA uses dark, medium, and light teal shades for failure, lifecycle, and traffic events.

The benchmark terminal shows each session's schedule progress, active occurrences, next event, and recent execution milestones. With `--output-mode agent`, these milestones stream as plain log lines prefixed by the task label.

Remote and Firecracker tasks execute the same scheduler in the lab-host daemon. The agent host waits for that timeline's release and completion signals; it does not establish a second clock. Firecracker restores a healthy environment image for scheduled tasks, with explicitly scheduled traffic sources deferred, even when case images are enabled. Different timelines can reuse that healthy image.

Firecracker synchronizes the restored guest's UTC clock with the host before exposing the environment. This keeps host and guest logs comparable while event deadlines continue to use the session's monotonic clock.

Infinite scheduler repetition and network updates are unsupported. Tasks without `schedule` receive execution defaults and use the same scheduler. Changing when the agent encounters failures requires a new benchmark revision; published releases are unchanged.
