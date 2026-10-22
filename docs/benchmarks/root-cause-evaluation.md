# Root-cause ground truth and scoring

This reference is for maintainers who define failures, integrate troubleshooting agents, or evaluate submissions. It specifies the resource catalog, ground-truth format, submission contract, scoring, and benchmark label materialization.

Implementation: [`rca/models.py`](../../src/nika/problems/rca/models.py) defines the data model; [`rca/inventory.py`](../../src/nika/problems/rca/inventory.py) builds the resource catalog; [`base.py`](../../src/nika/problems/base.py) derives ground truth; [`ownership.py`](../../src/nika/problems/ownership.py) defines submission owner-kind policy; [`task_server.py`](../../src/nika/mcp/servers/common/task_server.py) validates submissions; [`scoring.py`](../../src/nika/evaluator/scoring.py) computes metrics; [`migrate.py`](../../src/nika/workflows/benchmark/migrate.py) materializes labels in benchmark YAML.

## Root-cause contract

NIKA represents each independent injected fault as one `(resource_id, fault_type)` pair.

- `resource_id` identifies the mutated resource in the lab catalog.
- `fault_type` is a registered failure ID such as `link_down` or `bgp_asn_misconfig`. The task MCP server returns the accepted values from `list_avail_problems()`.

The resource catalog uses these canonical IDs:

| Resource kind | ID format | Example |
| --- | --- | --- |
| Node | `node/{name}` | `node/pc1` |
| Interface | `interface/{node}/{interface}` | `interface/pc1/eth0` |
| Link | `link/{sorted node:intf joined by --}` | `link/pc1:eth0--router1:eth0` |
| Kubernetes object | `k8s/{kind}/{namespace}/{name}` | `k8s/Service/kube-system/kube-dns` |

Owner kind follows the mutated catalog object. Controller-side cable faults (`link_down`, `link_flap`, `link_packet_corruption`) use the undirected link TP set. Interface removals, egress hooks, and queueing faults stay on `interface/...`.

Benchmark YAML stores the parsed resource fields (`kind` / `node` / `name`, or the k8s fields) plus `fault_type`. NIKA derives `resource_id` from those fields when it scores, injects, or accepts a submit. Agent submissions may send either `resource_id` or the same resource fields; `submit` always writes the constructed `resource_id`.

```yaml
root_causes:
  - resource:
      kind: link
      name: pc1:eth0--router1:eth0
    fault_type: link_down
```

Healthy ground truth uses `is_anomaly: false` and an empty `root_causes` list. Benchmark YAML may include `problem: healthy` rows for no-fault control cases; those rows skip injection and persist the same healthy ground truth. A multi-fault case contains one root-cause object for each injected fault source. Each object identifies the resource that the injector mutates. Symptoms and affected peers do not become root-cause labels.

Multi-fault benchmark rows use a `problems` list and nested `inject` maps. The normalized `problem` label is `problem_a+problem_b` for task labels and fingerprints.

```yaml
- scenario: dc_clos
  topo_size: s
  problems:
    - mtu_mismatch
    - icmp_frag_needed_filter_misconfiguration
  problem: mtu_mismatch+icmp_frag_needed_filter_misconfiguration
  inject:
    mtu_mismatch:
      host_name: leaf_router_0_1
      intf_name: eth2
      mtu: "500"
    icmp_frag_needed_filter_misconfiguration:
      host_name: leaf_router_0_1
  root_causes:
    - resource:
        kind: interface
        node: leaf_router_0_1
        name: eth2
      fault_type: mtu_mismatch
    - resource:
        kind: node
        node: leaf_router_0_1
      fault_type: icmp_frag_needed_filter_misconfiguration
```

CLI injection accepts multiple problem IDs and per-fault `--set` overrides as `problem.field=value`.

## Generate ground truth for a failure

Each concrete `ProblemBase` subclass implements `root_cause_resources(params)` next to `inject_fault(params)`. Return catalog resources with the helpers in [`rca/models.py`](../../src/nika/problems/rca/models.py) and [`rca/inventory.py`](../../src/nika/problems/rca/inventory.py).

```python
from nika.problems.rca.inventory import link_containing_endpoint


def root_cause_resources(self, params: MyFaultParams):
    return [link_containing_endpoint(self.net_env, params.host_name, params.intf_name)]
```

For interface-owned faults, use `interface_on` instead. `ProblemBase.get_ground_truth()` combines each returned resource with the class `root_cause_name` (the failure ID / `fault_type`). Failure authors should not maintain a second root-cause table.

The following paths derive labels from the same method:

- `nika failure inject` writes session ground truth.
- Catalog generation (`nika benchmark generate`) writes the candidate catalog.
- `freeze_release` materializes an explicitly supplied flat case file.
- `nika benchmark migrate` upgrades an existing case matrix.

Candidate options and frozen releases store materialized `root_causes` so reviewers can inspect labels. The candidate catalog stores one flat executable case per `failure.cases` entry with the same `root_causes` shape shown above. During injection, NIKA derives the labels again and rejects a case when the derived value differs from the materialized value.

## Submit a diagnosis

During the submission phase, the agent receives a frozen diagnosis report plus a
prompt-only catalog: `fault_ontology` entries `{id, description, owner_kind}` and
`resources` entries `{id, kind}`. `description` explains what each failure ID means
at a conceptual level and must not leak injection or differential diagnosis shortcuts;
`submit()` still requires the exact ontology `id` as `fault_type`.

`fault_ontology` always lists every registered fault type except the `healthy`
sentinel. The list is the same for `--release`, `--config`, and single-case runs,
so the candidate set never reveals which faults a case file contains.

1. Read the frozen resource inventory and fault ontology from the submission context.
2. Call `submit()` with the selected pairs.

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

The task server rejects an unknown `resource_id` or `fault_type` and does not write `submission.json`. Scoring reads `root_causes` only.

## Score a submission

Exact eval and leaderboard scoring contract. Implementation:
[`scoring.py`](../../src/nika/evaluator/scoring.py),
[`score_status.py`](../../src/nika/evaluator/score_status.py),
[`aggregate.py`](../../src/nika/workflows/leaderboard/aggregate.py).

### Per-trial rule-based metrics

NIKA writes `eval_metrics.json` when a trial closes (or when you run
`nika eval metrics`). Detection is binary. RCA metrics compare predicted and
expected sets. Duplicate pairs do not change a score.

| Metric group | Compared set | Fields |
| --- | --- | --- |
| Detection | `is_anomaly` bool | `detection_score` (`1.0` or `0.0`) |
| Joint RCA | `(resource_id, fault_type)` | `rca_precision`, `rca_recall`, `rca_f1` |
| Localization | `resource_id` | `localization_precision`, `localization_recall`, `localization_f1` |
| Fault type | `fault_type` | `fault_type_precision`, `fault_type_recall`, `fault_type_f1` |

`rca_accuracy` and `localization_accuracy` equal the corresponding recall
(backward-compatible aliases).

For a predicted set `P` and ground-truth set `T`:

- `precision = |P ∩ T| / |P|` (0 if `P` is empty)
- `recall = |P ∩ T| / |T|` (0 if `T` is empty)
- `F1 = 2 * precision * recall / (precision + recall)` (0 if both are 0)

Special case: when both `P` and `T` are empty, precision, recall, and F1
are all `1.0`. That is the correct score for a healthy case with an empty
`root_causes` submission. An agent that never submits must not reach this
path (see `score_status` below).

Unparseable agent `root_causes` score as `0.0` under `scored`. Unparseable
`is_anomaly` also scores `0.0`. Trace counters (`in_tokens`, `out_tokens`,
`steps`, `tool_calls`, `tool_errors`) are recorded separately.
`in_tokens` is uncached prompt tokens plus Anthropic cache creation and
cache read. OpenAI-style `prompt_tokens` already include cached tokens, so
those are not added again. Traces go through `agent.utils.usage.normalize_usage`.

Score columns are only values in `[0, 1]` or `null`. They never store `-1`.

### Trial `score_status`

Each finished trial stores `score_status` on `run.json`. Decision order asks
one question first: does `submission.json` exist?

1. **Has submission**
   - Grading succeeds → `scored` (score columns hold the computed `[0, 1]` values).
   - Ground truth missing/corrupt or the scorer raises → `grading_error`
     (score columns are `null`). Fix GT or scoring code and regrade. Do not
     re-run the agent.
2. **No submission**
   - Ground truth missing (env/inject never completed) or clear env / LLM API /
     MCP failure evidence → `infra_error` (score columns are `null`). Resume
     and `--retry-passes` re-run these trials. Any residual `infra_error` after
     retries rejects a leaderboard pack.
   - Otherwise (timeout, max steps, crash, early exit, failed `submit`
     validation, unknown cause) → `no_submission` (score columns are
     explicit `0.0`). Do not re-run.

| Status | Score columns | Primary contribution | Follow-up |
| --- | --- | --- | --- |
| `scored` | `[0, 1]` | actual value | — |
| `no_submission` | `0.0` | 0 | do not re-run |
| `infra_error` | `null` | 0 | auto-retry; residual rejects pack |
| `grading_error` | `null` | 0 | regrade; presence rejects pack |

Lifecycle `outcome` stays `success` or `agent_failed` for resume and pack:

- `scored` / `grading_error` with a submission → `success`
- `no_submission` / residual `infra_error` → `agent_failed`

If the agent already wrote `submission.json` and cleanup later hits an infra
fault, status stays on the submission path (`scored` or `grading_error`).

### Aggregate (leaderboard and `nika eval summary`)

Primary means use a fixed denominator
`N = case_count × n_trials`:

`mean = (sum of per-trial contributions) / N`

Each contribution is the trial's score when `score_status=scored`, and `0`
for every other status (including `null` scores and trials that never
landed). Missing expected trials contribute `0`.

Always report these as a pair:

- `submission_rate = n(scored) / N`
- `conditional_mean_*` over the `scored` subset only (`null` when
  `n(scored)=0`)

Also report:

- `status_counts` for each `score_status`
- fault-case means for `rca_f1` and `localization_f1`
- healthy-case mean for `detection_score` (RCA/localization are not meaningful
  for healthy controls)

Leaderboard pack/validate rejects a package when any trial has
`grading_error` or residual `infra_error`.

Example: three trials with primary scores `[1.0, no_submission, no_submission]`
yield primary mean `1/3`, `submission_rate=1/3`, and conditional mean `1.0`.

### Run offline eval

```shell
nika eval metrics --result_dir results/my_run
nika eval summary --result_dir results/my_run
```

`nika eval metrics` with `--result_dir` and no `--session_id` processes every
closed session under that directory. Benchmark runs already write metrics when
each case closes. `nika eval summary` uses the same aggregation as
`nika leaderboard pack`. CLI options: [nika eval](../operations/cli-reference.md#nika-eval).

## Materialize labels on a case matrix

`freeze_release` attaches `root_causes` when it writes release YAML from an explicit flat case file. Use `nika benchmark migrate` on an existing YAML matrix whose top level contains `cases`. Do not pass a candidate catalog or release `RELEASE.yaml` manifest.

```shell
uv run nika benchmark migrate \
  --input path/to/cases.yaml \
  --output /tmp/benchmark_labeled.yaml \
  --report /tmp/migration_report.yaml
```

The command rewrites each case from its `scenario`, `problem`, `topo_size`, and `inject` identity fields, then adds `root_causes`. These four fields continue to define the case fingerprint; migration does not copy unrelated per-row fields.

If a failure mapping reports an unresolved root cause, NIKA writes the output and report, marks the row with `root_causes_status: unresolved` and `root_causes_error`, then exits with status 1. Pass `--allow-unresolved` to exit with status 0 while retaining those markers. Benchmark execution recomputes labels for unresolved rows but cannot compare them with a materialized expected value.

Relabel an existing release by migrating its `dev.yaml` or `test.yaml` in place, then rewrite the manifest. Published releases remain immutable.
