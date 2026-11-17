# Ground truth and scoring

Every trial is scored against its ground truth: the failures NIKA injected. This page explains what an agent submits, how NIKA scores a submission, what each trial status means, and how the scores add up. To see where scoring fits in a run, read [how NIKA evaluates an agent](evaluation-pipeline.md) first.

Each injected failure is one root cause, written as a pair: the resource the failure changed (`resource_id`) and the failure's ID (`fault_type`), such as `link_down`.

## Submit a diagnosis

An agent submits two fields:

- `is_anomaly`: `true` when the agent reports a failure, and `false` for a healthy task.
- `root_causes`: the list of `(resource_id, fault_type)` pairs it identifies.

```json
{
  "is_anomaly": true,
  "root_causes": [
    {
      "resource_id": "link/pc1:eth0--router1:eth0",
      "fault_type": "link_down"
    }
  ]
}
```

The submission phase gives the agent the lists it must choose from: the fault types, with a description of each, and the resources in the environment. Use the IDs exactly as they appear there. Resource IDs follow these formats:

| Resource kind | ID format | Example |
| --- | --- | --- |
| Node | `node/{name}` | `node/pc1` |
| Interface | `interface/{node}/{interface}` | `interface/pc1/eth0` |
| Link | `link/{endpoints joined by --}` | `link/pc1:eth0--router1:eth0` |
| Kubernetes object | `k8s/{kind}/{namespace}/{name}` | `k8s/NetworkPolicy/word-ns/default-deny-ingress` |

The submit tool rejects an unknown `resource_id` or `fault_type`, and a duplicate pair, and it saves nothing for a rejected submission. After the consecutive-rejection limit (`agent.submit_reject_limit`, 5 by default), the agent can no longer submit. A trial without a valid submission ends as `no_submission` or another status described below.

## Score a submission

NIKA computes these metrics for each trial and writes them to `eval_metrics.json`.

| Metric group | Compared set | Fields |
| --- | --- | --- |
| Detection | `is_anomaly` | `detection_score`: `1.0` if correct, `0.0` if not |
| Joint RCA | `(resource_id, fault_type)` pairs | `rca_precision`, `rca_recall`, `rca_f1` |
| Localization | `resource_id` | `localization_precision`, `localization_recall`, `localization_f1` |
| Fault type | `fault_type` | `fault_type_precision`, `fault_type_recall`, `fault_type_f1` |

`rca_accuracy` and `localization_accuracy` repeat the recall values for older tools.

### How pairs match

- A fault type matches only when it is identical.
- A predicted resource matches a ground-truth resource when it is the same resource or a more specific part of it:

| Ground-truth resource | Also matched by |
| --- | --- |
| `node/{node}` | Any `interface/{node}/{ifname}` on that node |
| `link/{endpoints}` | `interface/{node}/{ifname}` for either endpoint of that link |

- Matching is one to one. Each ground-truth item counts once, so naming both ends of a faulty link gives one true positive and one false positive.
- A coarser answer does not match an interface fault. Naming only the node is not enough.
- Some distributed failures accept any of several locations. For `bgp_max_prefix_exceeded`, naming either the receiver or the peer that advertises the excess prefixes earns full RCA credit with the correct fault type. Localization counts those two locations once.

For a predicted set `P` and ground-truth set `T`, with `M` matched pairs:

- `precision = M / |P|`, or `0` if `P` is empty
- `recall = M / |T|`, or `0` if `T` is empty
- `F1 = 2 × precision × recall / (precision + recall)`, or `0` if both are `0`

### Healthy tasks

A healthy task has no root causes. Its answer earns full credit only when it says `is_anomaly: false` with an empty `root_causes` list. Reporting an anomaly with an empty list scores `0`.

### Invalid answers

An `is_anomaly` value that cannot be parsed scores `0`. A `root_causes` list that cannot be parsed scores `0` on every RCA metric. Both still count as a scored trial.

### Trial `score_status`

Each finished trial records a status in its `run.json`. NIKA first asks whether the agent wrote a submission, then decides the status.

| Status | Score columns | Meaning | What to do |
| --- | --- | --- | --- |
| `scored` | `[0, 1]` values | The submission was graded. | Nothing. |
| `no_submission` | `0.0` | No submission was recorded, because of a timeout, step limit, crash, rejected submit, or unknown cause. | Nothing. Do not rerun; failing to answer is the agent's result. |
| `infra_error` | `null` | Setup or injection did not finish, or the environment, LLM API, or MCP failed. | Resume the run. Retries rerun these trials. |
| `grading_error` | `null` | The submission exists, but the ground truth is missing or the scorer failed. | Repair the inputs and regrade. Do not rerun the agent. |

A trial with a submission that was written before a later infrastructure fault keeps its submission status. A leaderboard package is rejected while any trial has `grading_error` or an unresolved `infra_error`.

### Aggregate results

Primary means use a fixed denominator: the number of tasks multiplied by the trials each task needs. Each trial contributes its score when it is `scored`, and `0` in every other case, including missing trials.

```text
mean = (sum of per-trial contributions) / (tasks × trials per task)
```

Report these values together:

- `submission_rate`: scored trials divided by all expected trials.
- Conditional means: the same means over scored trials only, which are `null` when no trial is scored.
- Counts for each status.
- Mean `rca_f1` and `localization_f1` over fault tasks, and mean `detection_score` over healthy tasks.

Three trials with primary scores `[1.0, no_submission, no_submission]` give a primary mean of `1/3`, a submission rate of `1/3`, and a conditional mean of `1.0`.

### Run offline eval

```shell
nika eval metrics --result_dir results/my_run
nika eval summary --result_dir results/my_run
```

`nika eval metrics` scores every closed session under the directory with the installed NIKA's scoring code and overwrites the metrics. To reproduce an official release score, keep the metrics the release's engine wrote. `nika eval summary` aggregates the results as described above. Options: [`nika eval`](../operations/cli-reference.md#nika-eval).

## Reference

### Ground-truth format in benchmark YAML

A single-failure task lists its root cause like this:

```yaml
root_causes:
  - resource:
      kind: link
      name: pc1:eth0--router1:eth0
    fault_type: link_down
```

A healthy task has `is_anomaly: false` and an empty `root_causes` list. A `problem: healthy` row is a no-failure control: it injects nothing and keeps the same healthy ground truth.

A multi-failure task lists one root cause for each injected failure source. The root cause names the resource the failure changed, not the symptoms or the peers that see them:

```yaml
- scenario: dc_clos
  topo_size: s
  problems:
    - mtu_mismatch
    - icmp_frag_needed_filter_misconfiguration
  problem: mtu_mismatch+icmp_frag_needed_filter_misconfiguration
  inject:
    mtu_mismatch:
      host_name: leaf_2
      intf_name: eth3
      mtu: "500"
    icmp_frag_needed_filter_misconfiguration:
      host_name: leaf_2
  root_causes:
    - resource:
        kind: interface
        node: leaf_2
        name: eth3
      fault_type: mtu_mismatch
    - resource:
        kind: node
        node: leaf_2
      fault_type: icmp_frag_needed_filter_misconfiguration
```

When you inject several failures with `nika failure inject`, prefix each `--set` override with its failure ID, as in `mtu_mismatch.host_name=leaf_2`.

### Derive labels for a case matrix

NIKA derives the root causes from each failure's injection parameters. Use `nika benchmark migrate` to write those labels into an existing YAML matrix. The input must have a top-level `cases` list. Do not pass a candidate catalog or a release manifest.

```shell
uv run nika benchmark migrate \
  --input path/to/cases.yaml \
  --output /tmp/benchmark_labeled.yaml \
  --report /tmp/migration_report.yaml
```

Each case keeps its `scenario`, `problem`, `topo_size`, and `inject` fields, which define its identity, and gains `root_causes`. A case whose failure cannot resolve to a resource is written with `root_causes_status: unresolved` and an error, and it is listed in the report. The command then fails. Pass `--allow-unresolved` to write the output anyway and keep those markers. Published releases remain immutable.

### For failure authors

Each failure returns its root cause resources from `root_cause_resources(params)`, using the helpers `link_containing_endpoint` for cable faults and `interface_on` for interface faults. NIKA combines them with the failure's ID, so do not keep a second table of root causes. See [Create benchmark tasks](../development/creating-benchmark-tasks.md#write-an-injectable-failure).

Links are owned by the mutated catalog object. Controller-side cable faults (`link_down`, `link_flap`, `link_packet_corruption`) use the link. Interface removals, egress hooks, and queueing faults use the interface.

Maintainers: the data model is in [`src/nika/problems/rca/`](../../src/nika/problems/rca/), scoring is in [`src/nika/evaluator/scoring.py`](../../src/nika/evaluator/scoring.py), and the submit validation is in [`src/nika/mcp/servers/common/task_server.py`](../../src/nika/mcp/servers/common/task_server.py).

Changes to these scoring rules or to root-cause identity are benchmark contract changes, and they need a new benchmark revision under the [versioning policy](../../VERSIONING.md).
