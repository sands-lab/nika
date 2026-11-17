# Oracle solver

A task is fair only if a careful operator could solve it with the tools the agent gets. The `oracle` agent checks this for every task. For each injected failure, it follows a reference diagnosis, called a playbook: the commands an operator would run, the observation each command must show, and the evidence that identifies the root cause. It runs the steps through the same [MCP tools](../agents/mcp-servers.md) as any agent, and it submits a root cause only when the evidence holds.

A task the oracle cannot solve points to a failure the agent cannot observe, or to a defect in the task or its scoring.

The oracle knows the injected failure, so its scores are not agent results. Use it to validate tasks and to read a worked diagnosis for each one, not to compare agents.

## Check a benchmark release

Run these commands from the repository root on a host set up by [Installation](../operations/installation.md). The oracle runs inside the NIKA process, so it needs no LLM credentials and no agent sandbox.

```shell
uv run nika benchmark run --release 0.2.0 --split test --agent oracle
```

Solvable fault tasks score 1.0 for detection and RCA, and healthy tasks score 1.0 for detection. To run one trial, add `--task-id {task_id}__t01`. To run the oracle against a session where you injected a failure yourself, use `uv run nika agent run --agent oracle`.

## Read a task's reference diagnosis

`nika benchmark describe --oracle` prints a task's reference diagnosis without starting its environment:

```shell
uv run nika benchmark describe enterprise_branch__link_down__hcecf68b8564f \
  --release 0.2.0 --split test --oracle
```

```yaml
oracle:
- fault_type: link_down
  finding: The link on br1_corp_pc:eth0 has no carrier, so every path across it is cut.
  steps:
  - step: 1
    purpose: 'Triage: measure reachability and latency across the endpoint mesh.'
    call:
      tool: run_pingmesh_snapshot
      args: {}
  - step: 2
    purpose: List interface states and routes on br1_corp_pc, an endpoint of the unreachable paths.
    call:
      tool: exec_shell
      args:
        host_name: br1_corp_pc
        command: ip -br link; ip route
        timeout: 15
  - step: 3
    purpose: Check the operational state of br1_corp_pc:eth0.
    call:
      tool: exec_shell
      args:
        host_name: br1_corp_pc
        command: ip -d link show dev eth0; ip -br addr show dev eth0
        timeout: 15
    expect: br1_corp_pc:eth0 is operationally down
    evidence: true
  submit:
  - resource_id: link/br1_corp_pc2:eth0--br1_corp_pc3:eth0--br1_corp_pc4:eth0--br1_corp_pc:eth0--br1_edge:eth0
    fault_type: link_down
```

Playbooks that read the state of a running environment, such as those for Containerlab tasks, cannot be built offline. For these tasks the command prints `unavailable offline`, so run the oracle on a live session instead.

## Read the oracle's report

Each trial directory contains an `oracle.json` file:

- `solved` is `true` when the oracle confirmed every injected failure.
- `faults` has one entry per failure, with its `fault_type`, whether it was `confirmed`, its `finding`, and its `steps`.
- Each step records its purpose, the tool and arguments it called, the expected observation, whether the observation held, and an output excerpt.
- `failed_evidence` lists the numbers of the evidence steps that did not hold.

The diagnosis report that `nika inspect` shows presents the same steps in readable form.

## How the oracle solves a task

1. It rebuilds each injected failure from the session's injection record and asks the failure for its playbook.
2. It measures the symptom with a Pingmesh snapshot, then runs the playbook steps in order. A step that checks state which settles after injection, or an intermittent fault, is retried until its observation holds.
3. It submits the ground-truth root causes of every failure whose evidence steps all hold. In a multi-failure task, it can submit the confirmed failures and leave out an unconfirmed one. That earns partial RCA credit, and `oracle.json` reports `solved: false`. When no failure is confirmed, it does not submit, so the trial ends as `no_submission` and scores 0.

For a healthy task, the playbook is a Pingmesh snapshot that must report no anomalies. The oracle then submits `is_anomaly: false`, and otherwise it does not submit.

## Write a playbook for a new failure

Each failure implements `oracle_playbook(params)` next to `inject_fault` and `verify_fault`. Build the playbook from the step helpers in [`support/oracle.py`](../../src/nika/problems/support/oracle.py), such as `shell`, `ping`, `nft_rule`, `iface_down`, `kubectl`, `p4rt`, and `onos`, with the checks `contains`, `lacks`, and `all_of`. This is the `link_down` playbook from [`link.py`](../../src/nika/problems/link_interface/link.py):

```python
def oracle_playbook(self, params: LinkFailureParams) -> OraclePlaybook:
    intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
    host = params.host_name
    steps = [
        shell(
            host,
            "ip -br link; ip route",
            f"List interface states and routes on {host}, an endpoint of the unreachable paths.",
        ),
        iface_down(host, intf),
    ]
    peer = self._peer_endpoint(host, intf)
    if peer is not None:
        steps.append(iface_down(*peer, evidence=False))
    return OraclePlaybook(
        finding=f"The link on {host}:{intf} has no carrier, so every path across it is cut.",
        steps=steps,
    )
```

A playbook must prove that the task can be solved, not restate the answer:

- Collect evidence only through the agent's MCP tools. Injection parameters and the topology can decide where to look, but not what counts as an observation.
- Mark as evidence only the observations that tell this failure apart from similar ones in the [fault ontology](root-cause-evaluation.md#submit-a-diagnosis). For example, `dns_service_down` needs no `named` process and no port 53 listener, while `dns_port_blocked` needs `named` running behind a port 53 drop rule.
- Keep the steps read-only. Probe traffic is fine; configuration changes and restarts are not.
- Cover every backend and device profile the failure runs on. Test the playbook on a real task with `uv run nika benchmark run --release 0.2.0 --split test --agent oracle --task-id {task_id}`, or with `uv run nika agent run --agent oracle` on a session where you injected the failure.

## Reference: release 0.2.0 results

On 2026-10-31, with one trial per task, the oracle solved 166 of the 169 tasks in release 0.2.0 with detection and RCA scores of 1.0. Rerunning the other three solved them too: `k8s_lab__bgp_asn_misconfig__he4a55dc53b83` after a playbook fix, and the two `receiver_resource_contention` tasks (`llmd_lab__receiver_resource__h494b49e3caef` and `dc_clos__receiver_resource_c__ha2be5b357067`) after a correction to the injected fault. In release 0.2.0 that fault limits the receiver to 0.02 CPU, so any command run on the receiver can take more than a minute. The correction raises the limit to 0.05 CPU, which still roughly halves the download rate. Because it changes the injected fault, the correction belongs to the next benchmark revision under the [versioning policy](../../VERSIONING.md).
