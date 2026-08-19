# Root-cause ground truth and scoring

This reference is for maintainers who define failures, integrate troubleshooting agents, or evaluate submissions. It specifies the resource catalog, ground-truth format, submission contract, scoring, and benchmark label materialization.

Implementation: [`root_cause.py`](../src/nika/problems/root_cause.py) defines the data model; [`topology_inventory.py`](../src/nika/problems/topology_inventory.py) builds the resource catalog; [`problem_base.py`](../src/nika/problems/problem_base.py) derives ground truth; [`task_server.py`](../src/nika/service/mcp_server/common/task_server.py) validates submissions; [`scoring.py`](../src/nika/evaluator/scoring.py) computes metrics; [`migrate.py`](../src/nika/workflows/benchmark/migrate.py) materializes labels in benchmark YAML.

## Root-cause contract

NIKA represents each independent injected fault as one `(resource_id, fault_type)` pair.

- `resource_id` identifies the mutated resource in the lab catalog.
- `fault_type` is a registered failure ID such as `link_down` or `bgp_asn_misconfig`. The task MCP server returns the accepted values from `list_avail_problems()`.

The resource catalog uses these canonical IDs:

| Resource kind | ID format | Example |
| --- | --- | --- |
| Node | `node/{name}` | `node/pc1` |
| Interface | `interface/{node}/{interface}` | `interface/pc1/eth0` |
| Kubernetes object | `k8s/{kind}/{namespace}/{name}` | `k8s/Service/kube-system/kube-dns` |

Schema version 3 writes `failure_domain`, `cause`, `symptom`, `scope`, `temporal`, and `impact` as statistics fields in `ground_truth.json`. `root_cause_category` remains a compatibility alias of `failure_domain`. NIKA does not include taxonomy metadata in the RCA key.

Benchmark YAML stores the parsed resource fields (`kind` / `node` / `name`, or the k8s fields) plus `fault_type`. NIKA derives `resource_id` from those fields when it scores, injects, or accepts a submit. Agent submissions may send either `resource_id` or the same resource fields; `submit` always writes the constructed `resource_id`.

```yaml
root_causes:
  - resource:
      kind: interface
      node: pc1
      name: eth0
    fault_type: link_down
```

Healthy ground truth uses `is_anomaly: false` and an empty `root_causes` list. A multi-fault case contains one root-cause object for each injected fault source. Each object identifies the resource that the injector mutates. Symptoms and affected peers do not become root-cause labels.

## Generate ground truth for a failure

Each concrete `ProblemBase` subclass implements `root_cause_resources(params)` next to `inject_fault(params)`. Return catalog resources with the helpers in [`root_cause.py`](../src/nika/problems/root_cause.py) and [`topology_inventory.py`](../src/nika/problems/topology_inventory.py).

```python
from nika.problems.topology_inventory import interface_on


def root_cause_resources(self, params: MyFaultParams):
    return [interface_on(self.net_env, params.host_name, params.intf_name)]
```

`ProblemBase.get_ground_truth()` combines each returned resource with the class `root_cause_name` (the failure ID / `fault_type`). Failure authors should not maintain a second root-cause table.

The following paths derive labels from the same method:

- `nika failure inject` writes session ground truth.
- `benchmark/generate_benchmark.py` writes the working matrices.
- `benchmark/generate_benchmark.py --release VERSION` and `freeze_release` write frozen release case files.
- `nika benchmark migrate` upgrades an existing case matrix.

Working matrices and frozen releases store materialized `root_causes` so reviewers can inspect labels. During injection, NIKA derives the labels again and rejects a case when the derived value differs from the materialized value.

## Submit a diagnosis

During the submission phase, an agent uses the task MCP tools in this order:

1. Call `list_resources()` to get the current lab's node and interface IDs. Kubernetes sessions also include live Service and NetworkPolicy IDs when the Kubernetes API is available.
2. Call `list_avail_problems()` to get the registered fault types.
3. Call `submit()` with the selected pairs.

```json
{
  "is_anomaly": true,
  "root_causes": [
    {
      "resource_id": "interface/pc1/eth0",
      "fault_type": "link_down"
    }
  ]
}
```

The task server rejects an unknown `resource_id` or `fault_type` and does not write `submission.json`. Scoring reads `root_causes` only.

## Score a submission

Scoring compares the predicted and expected sets of `(resource_id, fault_type)` pairs. Duplicate pairs do not change a score.

| Metric group | Compared set | Output fields |
| --- | --- | --- |
| Joint RCA | `(resource_id, fault_type)` | `rca_precision`, `rca_recall`, `rca_f1` |
| Localization | `resource_id` | `localization_precision`, `localization_recall`, `localization_f1` |
| Fault type | `fault_type` | `fault_type_precision`, `fault_type_recall`, `fault_type_f1` |

`rca_accuracy` and `localization_accuracy` copy their corresponding recall values for schema compatibility. Detection and trace counters (`in_tokens`, `out_tokens`, `steps`, `tool_calls`, and `tool_errors`) remain separate fields in `eval_metrics.json`. `in_tokens` is uncached prompt tokens plus Anthropic cache creation and cache read. OpenAI-style `prompt_tokens` already include cached tokens, so those are not added again. All agent traces go through `agent.utils.usage.normalize_usage`.

```shell
nika eval metrics
```

With `--result_dir` and no `--session_id`, the command processes every closed session under that directory. Benchmark runs write these metrics when each case closes.

## Materialize labels on a case matrix

`freeze_release` and `benchmark/generate_benchmark.py --release VERSION` attach `root_causes` when they write release YAML. Use `nika benchmark migrate` on an existing YAML matrix whose top level contains `cases`. Do not pass a release `RELEASE.yaml` manifest.

```shell
uv run nika benchmark migrate \
  --input path/to/cases.yaml \
  --output /tmp/benchmark_labeled.yaml \
  --report /tmp/migration_report.yaml
```

The command rewrites each case from its `scenario`, `problem`, `topo_size`, and `inject` identity fields, then adds `root_causes`. These four fields continue to define the case fingerprint; migration does not copy unrelated per-row fields.

If a failure mapping reports an unresolved root cause, NIKA writes the output and report, marks the row with `root_causes_status: unresolved` and `root_causes_error`, then exits with status 1. Pass `--allow-unresolved` to exit with status 0 while retaining those markers. Benchmark execution recomputes labels for unresolved rows but cannot compare them with a materialized expected value.

Do not run `benchmark/generate_benchmark.py --release 0.1.0` to relabel the published suite. That path re-selects Test instances from `benchmark_full.yaml` and can change case identity. Relabel an existing release by migrating its `dev.yaml` / `test.yaml` in place, then rewriting `RELEASE.yaml` pins and digest.
