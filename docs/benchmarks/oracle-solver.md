# Oracle solver

A benchmark task is only fair if a careful operator could solve it with the tools the agent gets. The `oracle` agent checks that for every task. For each injected failure it follows the failure's playbook, a reference diagnosis: the commands an operator would run, the observation each one must show, and the evidence that identifies the root cause. It runs those steps through the same [MCP tools](../agents/mcp-servers.md) as any agent, and submits the root cause only when the evidence holds. A task the oracle cannot solve points at a fault the agent cannot observe, or at a defect in the task or its scoring.

The oracle knows the injected failure and its parameters, so its scores are not agent results. Use it to validate tasks and to read a worked diagnosis for each one, not to compare agents.

## Run the oracle on the provisional 0.3.0 benchmark

The 0.3.0 benchmark is provisional and has not been officially released. Run these commands from the repository root on a lab host prepared by [Installation](../operations/installation.md). The oracle runs in the NIKA process, so it needs neither LLM credentials nor an agent sandbox:

```shell
uv run nika benchmark run --release 0.3.0 --split test --agent oracle
```

Every solvable fault task scores 1.0 for detection and RCA, and every healthy task scores 1.0 for detection. To run one trial, add `--task-id {task_id}__t01`. To run the oracle on a session where you injected a failure yourself, run `uv run nika agent run --agent oracle`.

Each trial directory also contains `oracle.json`. `solved` is true when the oracle confirmed every injected failure. Each entry in `faults` lists its steps (tool call, expected observation, whether it held, and an output excerpt) and `failed_evidence`, the numbers of the evidence steps that did not hold. A readable version of the same steps is the diagnosis report, which `nika inspect` shows.

## Read a task's reference diagnosis

`nika benchmark describe --oracle` prints a task's reference diagnosis without starting the environment:

```shell
uv run nika benchmark describe dc_clos__link_down__s__netfl__h907e0a095756 \
  --release 0.3.0 --split test --oracle
```

The output lists each step's tool, arguments, expected observation and evidence flag, followed by the root causes that confirmed evidence permits the oracle to submit.

Playbooks that read the running environment's state, such as those for Containerlab tasks, cannot be built offline; for them `describe --oracle` prints `unavailable offline`, so run the oracle on a live session instead.

## How the oracle solves a task

1. It rebuilds each injected failure from the session's injection record and asks the failure for its playbook.
2. It maps authored device reads to `device_read` using the scenario's declared node roles and capabilities. Unsupported shell evidence stays in the playbook and fails access checks; the oracle keeps every evidence requirement.
3. It queries recorded endpoint probe results with `prometheus_query`, then runs the playbook steps in order. A step whose observation takes time to settle after injection, or that checks an intermittent fault, is retried until the observation holds.
4. It submits the ground-truth root causes of every failure whose evidence steps all hold. In a multi-fault task it can therefore submit the confirmed faults and leave out an unconfirmed one, which earns partial RCA credit while `oracle.json` reports `solved: false`. When no failure is confirmed, it does not submit, so the trial ends as `no_submission` and scores 0.

A healthy task's playbook queries recorded endpoint probes and requires them to match the reachability policy; the oracle then submits `is_anomaly: false`, and otherwise does not submit.

## Write a playbook for a new failure

Each failure implements `oracle_playbook(params)` next to `inject_fault` and `verify_fault`. Build it from the step templates in [`support/oracle.py`](../../src/nika/problems/support/oracle.py), such as `shell`, `ping`, `nft_rule`, `interface_state`, `kubectl`, `p4rt`, and `onos`, and the checks `contains`, `lacks`, and `all_of`. The [link-down and flap playbooks](../../src/nika/problems/link_interface/link.py) show how to read the selected attachment through its platform's tools and check carrier over time.

Follow these rules so the playbook proves solvability rather than restating the answer:

- Collect evidence only through the agent's MCP tools. Injection parameters and the topology may choose where to look, but not what is observed.
- Mark as evidence only the observations that tell this failure apart from similar ones in the [fault ontology](root-cause-evaluation.md#submit-a-diagnosis). For example, `dns_service_down` needs no `named` process and no port 53 listener, while `dns_port_blocked` needs `named` running behind a port 53 drop rule.
- Keep steps read-only. Probe traffic is fine; configuration changes and restarts are not.
- Cover every backend and device profile the failure runs on. Test the playbook on a real task with `uv run nika benchmark run --release 0.3.0 --split test --agent oracle --task-id {task_id}`, or with `uv run nika agent run --agent oracle` on a session where you injected the failure.

When a check depends on an earlier observation, save that output with `save_as` and use `compare(description, predicate)`. The predicate receives the current output and the mapping of saved outputs. Keep comparisons and counter arithmetic in the predicate. Use repeated steps or `attempts` for temporal observations; device commands contain only the read itself.

Link-down evidence requires carrier to remain down throughout a 12-second observation. Link-flap evidence requires both carrier states and separate bursts of packet loss. A single down sample cannot distinguish these failures.

A more-specific BGP hijack requires live evidence of the legitimate owner's covering prefix, along with the hijacker's local origin, export, and the observer's selected path. Declared prefix ownership selects the router and prefix to inspect; it does not replace the live observation.

Select the prefix in SR Linux route and BGP reads. Read the chosen best path's referenced attribute set separately. Full-table JSON can exceed the MCP output limit and become an unparseable preview.
