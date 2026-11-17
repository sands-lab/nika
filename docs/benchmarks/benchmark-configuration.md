# Benchmark configuration

Use this page to run a published benchmark release or your own tasks, and to check which failures each environment supports. The [reference](#reference) at the end holds file formats and run limits.

Terms follow the [glossary](../../GLOSSARY.md). A **task** combines a network environment, a network incident, and a network traffic pattern. A **trial** is one scored run of a task.

## Choose what to run

| To | Run |
| --- | --- |
| Run a published release | `nika benchmark run --release 0.2.0 --split test --result_dir results/my-run` |
| Run one task file | `nika benchmark run --config config/tasks/multi-failure.example.yaml` |
| Run your own set of tasks | `nika benchmark run --config my-benchmark.yaml` |
| Check which failures an environment supports | Read the [coverage matrix](#coverage-matrix-scenario--failure) |

## Run a published release

A release is a published, unchanging set of tasks and the rules that score them. Each release has a Dev split and a Test split.

1. List the releases, and the task IDs of one split:

   ```shell
   nika benchmark releases
   nika benchmark list --release 0.2.0 --split test
   ```

2. Run a split into a new result directory:

   ```shell
   nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
   ```

3. Submit the finished run to the leaderboard with the [leaderboard submission guide](leaderboard-submission.md).

`--split` selects `dev` or `test`. Without it, NIKA uses the split that the release manifest, `RELEASE.yaml`, names as default; for `0.2.0` that is `test`. The run covers every trial the release defines for the split.

Run again with the same `--result_dir` to resume. NIKA skips finished trials and retries the ones that ended with an infrastructure or model endpoint failure (see [trial outcomes](#trial-outcomes)).

> [!NOTE]
> A release whose `RELEASE.yaml` has an `engine` block runs on its [benchmark engine](../../specs/benchmark-contracts.md#b-lifecycle-protocol), a container built from the Git reference in `engine.tag`. Its environments, failures, MCP tools, and scoring stay those of the release as NIKA changes. The first run builds that image; `nika engine build <tag>` builds it ahead of time. Firecracker execution and NIKA Remote cannot run these releases yet.
>
> A release without an `engine` block, and every ad-hoc run, uses the NIKA you have installed. Its results can change when you update NIKA.

Release `0.1.0` runs with `--release 0.1.0`. [Run release 0.1.0](../compat/release-0.1.0.md) describes its restored environments and known limitations.

### Read the run output

Each run writes one directory:

| Path | Contents |
| --- | --- |
| `run.json` | The run settings: release, split, agent, model, number of trials, and run ID |
| `RELEASE.lock.json` | The release identity the run is scored against |
| `trials/{task_id}__tNN/` | One counted trial. `task_id` is the public task ID; `NN` is the trial number |

Active progress is recorded under `runtime/benchmark_runs/`.

To run only some tasks, pass `--task-id` one or more times, with a task ID or a trial directory name.

### Trial outcomes

Each trial ends with one outcome. [How NIKA evaluates an agent](evaluation-pipeline.md) explains how outcomes become scores.

| Outcome | Scored | Meaning | Next step |
| --- | --- | --- | --- |
| `success` | Yes | The agent submitted an answer, and NIKA scored it. | None |
| `agent_failed` | Yes, as zero | The agent gave no valid submission, or the trial timed out during agent work. | None; it counts |
| `endpoint_failed` | No | The model endpoint failed, for example with a refused connection or a provider 5xx. | `--resume` retries it |
| `infra_failed` | No | NIKA or the host failed after the ground truth was written and before the agent started. | `--resume` retries it |

## Run your own tasks

### Run one task file

A task file holds one complete task: its environment, failures, traffic, and schedule. Run it directly with `--config`:

```shell
nika benchmark run --config config/tasks/multi-failure.example.yaml
```

### Run a set of tasks

Write a benchmark YAML whose `cases` list gives task files, with paths relative to the benchmark file:

```yaml
cases:
  - tasks/multi-failure.example.yaml
  - tasks/healthy.example.yaml
```

Run it with `nika benchmark run --config my-benchmark.yaml`. The [task file layout](scheduling.md#reuse-task-files) shows how to organize the files. `nika benchmark migrate` expands the references and writes ground truth into one complete `cases` file, which is easier to share.

### Narrow a run

| Option | Effect |
| --- | --- |
| `--task-id` | Run only these tasks. Repeatable. |
| `--device-profile` | Run only rows for these router platforms. Repeatable. |
| `--skip-licensed` | Leave out rows that need licensed vendor images. |
| `--continue-on-error`, `--abort-on-error` | Keep going after a failed trial, or stop at the first one. |
| `--case-timeout` | Stop each trial after this many seconds. |
| `--batch-size`, `--heavy-batch-size` | Run this many light trials, or heavy trials, at once. |

Defaults for the last three options come from `benchmark.*` in `config/nika.yaml`. Ad-hoc batches stop at the first failed trial unless `benchmark.continue_on_error` is set. Release runs continue past failed trials unless you pass `--abort-on-error`.

## Working matrix

The working matrix is the executable task space that NIKA generates from its environment and failure registries. `nika benchmark run` without `--config` or `--release` runs all of it. It is large, so use a subset for most work.

1. Select a compact subset. This writes `benchmark/working/cases.yaml`:

   ```shell
   nika benchmark select
   ```

2. Run the subset:

   ```shell
   nika benchmark run --config benchmark/working/cases.yaml
   ```

Rows for IOS XR and RouterOS need licensed vendor images. [Router platform profiles](#router-platform-profiles) lists them and the other platform rules.

## Check coverage

The [coverage matrix](#coverage-matrix-scenario--failure) lists every failure against each environment and router platform. Read the legend at the top of the matrix, find the environment's column group, then find the failure's row. The matrix also shows which cells the 0.2.0 release includes.

## Coverage matrix (scenario × failure)

Cells mark **capability**. A failure is compatible when its tags and deploy constraints match the scenario and ISP `topo`/`igp`/`bgp_mode` profile. Candidate generation then validates semantic inject targets. Release membership comes from `benchmark/releases/0.2.0/` (dev + test).

Each table has two header rows: scenario, then config (when the scenario has more than one). Cells: blank = incompatible, `○` = compatible, `●` = compatible and in release `0.2.0`. Tables are split by failure subsystem.

Regenerate after registry or TAGS changes:

```shell
uv run python scripts/render_coverage_matrix.py --write-docs
```

### Link & Interface

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>link_capacity_bottleneck</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>link_detach</code></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>link_down</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>link_flap</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
</tr>
<tr>
<td><code>link_packet_corruption</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
</tr>
<tr>
<td><code>silent_egress_packet_loss</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

### Routing & Control Plane

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>bgp_address_family_not_activated</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_asn_misconfig</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_blackhole_community_leak</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_max_prefix_exceeded</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_md5_password_mismatch</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_missing_route_advertisement</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_prefix_list_length_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_rpki_invalid_route_leak</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_session_policy_missing</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_ttl_security_mismatch</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>isis_authentication_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>isis_circuit_level_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>isis_metric_style_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_area_misconfiguration</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_area_range_suppression</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_duplicate_router_id</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_hello_interval_mismatch</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_neighbor_missing</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_network_type_mismatch</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_stub_area_mismatch</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>routing_service_down</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>vrf_route_target_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

### Forwarding, Encapsulation & Policy

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>arp_acl_block</code></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
</tr>
<tr>
<td><code>bgp_acl_block</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>bmv2_switch_down</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>device_flow_blackhole</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
</tr>
<tr>
<td><code>device_forwarding_packet_corruption</code></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>dns_port_blocked</code></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>flow_rule_loop</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>flow_rule_shadowing</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>host_static_blackhole</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>http_acl_block</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
</tr>
<tr>
<td><code>icmp_acl_block</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>icmp_frag_needed_filter_misconfiguration</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>int_insufficient_mtu_headroom</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_networkpolicy_deny</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_vxlan_neighbor_stale</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>mtu_mismatch</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_acl_block</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_action_selector_member_misconfig</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_ecmp_group_member_missing</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_table_entry_misconfig</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_table_entry_missing</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_table_resource_exhaustion</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_tcam_entry_corruption</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4runtime_partial_write</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4runtime_pipeline_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>rib_fib_inconsistency</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>vrf_dscp_remarking</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>wireguard_allowed_ips_misconfiguration</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>wireguard_peer_key_misconfiguration</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

### Service Networking

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>k8s_clusterip_routing_broken</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_ingress_class_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_ingress_rate_limit_misconfig</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_service_selector_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_service_target_port_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>lb_advertisement_missing</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>lb_connection_state_exhaustion</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>lb_pending_connection_update_race</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>load_balancer_overload</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>nat_mapping_removed_without_drain</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>snat_port_pool_exhaustion</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

### Management & Orchestration Plane

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>k8s_coredns_forward_loop</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_deployment_replicas_zero</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_node_noexecute_taint</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_readiness_probe_misconfig</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_worker_apiserver_partition</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>sdn_controller_crash</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>southbound_port_block</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>southbound_port_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>switch_datapath_id_conflict</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
</tr>
</tbody>
</table>

### Addressing, Neighbor & Naming

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>dhcp_missing_subnet</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_service_down</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_lookup_latency</code></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_record_error</code></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_service_down</code></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_dns</code></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_gateway</code></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_ip</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>host_incorrect_netmask</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_ip_conflict</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>host_missing_ip</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>k8s_coredns_isolated</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>mac_address_conflict</code></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
</tr>
</tbody>
</table>

### Endpoint & Application

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>receiver_resource_contention</code></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
</tr>
<tr>
<td><code>sender_resource_contention</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">●</td>
</tr>
<tr>
<td><code>tls_certificate_expired</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

### Traffic, Queueing & Resource

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>incast_traffic_network_limitation</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>overlay_qos_ef_undersized</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>p4_ecn_threshold_misconfiguration</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>tcp_receive_window_limited</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

### Security

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th rowspan="2">campus</th>
<th rowspan="2">clos</th>
<th rowspan="2">enterprise</th>
<th rowspan="2">iosxr_simple_bgp</th>
<th colspan="4">isp_abilene</th>
<th rowspan="2">isp_abilene_ebgp_rpki</th>
<th rowspan="2">isp_abilene_ebgp_rtbh</th>
<th rowspan="2">isp_dfn-bwin_ebgp_rtbh</th>
<th colspan="4">isp_france</th>
<th rowspan="2">isp_geant_ebgp_rpki</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">min3clos</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
<th rowspan="2">routeros_simple_bgp</th>
<th rowspan="2">sdn_l3_clos</th>
</tr>
<tr>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
<th>isis</th>
<th>ospf</th>
<th>ibgp_rr</th>
<th>ebgp</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>arp_cache_poisoning</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
</tr>
<tr>
<td><code>bgp_hijacking</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_spoofed_dns</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_spoofed_gateway</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_spoofed_subnet</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>tcp_syn_flood_attack</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>web_dos_attack</code></td>
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
</tr>
</tbody>
</table>

### Native router profiles

IOS XR and RouterOS deployments of the environments that declare them (`isp` stands for every base ISP scenario). Blank = incompatible, `◆` = the failure also targets the profile's routers (its `native_router_operations` are all implemented by the vendor provider), `○` = it targets only the FRR/Linux nodes of the deployment, which may have none when every router converts. A suffix shows the last execution result from `benchmark/working/validation/device_profiles.yaml`: `✓` validated, `✗` failed, `–` unsupported; no suffix = not run.

<table>
<thead>
<tr>
<th rowspan="2">Failure</th>
<th colspan="2">campus</th>
<th colspan="2">clos</th>
<th colspan="2">enterprise</th>
<th colspan="2">isp</th>
</tr>
<tr>
<th>iosxr</th>
<th>routeros</th>
<th>iosxr</th>
<th>routeros</th>
<th>iosxr</th>
<th>routeros</th>
<th>iosxr</th>
<th>routeros</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>link_capacity_bottleneck</code></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆✗</td>
<td align="center">◆✓</td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>link_detach</code></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✗</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>link_down</code></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>link_flap</code></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>link_packet_corruption</code></td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_address_family_not_activated</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>bgp_asn_misconfig</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>bgp_max_prefix_exceeded</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>bgp_md5_password_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>bgp_missing_route_advertisement</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>bgp_prefix_list_length_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_session_policy_missing</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>bgp_ttl_security_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>isis_authentication_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>isis_circuit_level_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>isis_metric_style_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>ospf_area_misconfiguration</code></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>ospf_area_range_suppression</code></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_duplicate_router_id</code></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>ospf_hello_interval_mismatch</code></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>ospf_neighbor_missing</code></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✗</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>ospf_network_type_mismatch</code></td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">◆</td>
</tr>
<tr>
<td><code>ospf_stub_area_mismatch</code></td>
<td align="center">◆</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>routing_service_down</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>vrf_route_target_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>arp_acl_block</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_acl_block</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>device_flow_blackhole</code></td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>device_forwarding_packet_corruption</code></td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center">○</td>
<td align="center">◆</td>
<td align="center">○✓</td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_port_blocked</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_static_blackhole</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>http_acl_block</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>icmp_acl_block</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
</tr>
<tr>
<td><code>icmp_frag_needed_filter_misconfiguration</code></td>
<td align="center">◆</td>
<td align="center">◆✓</td>
<td align="center">◆</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>mtu_mismatch</code></td>
<td align="center">◆</td>
<td align="center">◆✓</td>
<td align="center">◆</td>
<td align="center">◆✓</td>
<td align="center">◆✗</td>
<td align="center">◆✓</td>
<td align="center">◆</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>ospf_acl_block</code></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✗</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>vrf_dscp_remarking</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>wireguard_allowed_ips_misconfiguration</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>wireguard_peer_key_misconfiguration</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>load_balancer_overload</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>nat_mapping_removed_without_drain</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>snat_port_pool_exhaustion</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center">◆✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_missing_subnet</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_service_down</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_lookup_latency</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_record_error</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dns_service_down</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_dns</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_gateway</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_ip</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_netmask</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_ip_conflict</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_missing_ip</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>mac_address_conflict</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>receiver_resource_contention</code></td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>sender_resource_contention</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>tls_certificate_expired</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>overlay_qos_ef_undersized</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>tcp_receive_window_limited</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>arp_cache_poisoning</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_hijacking</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✗</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
<td align="center">◆✓</td>
</tr>
<tr>
<td><code>dhcp_spoofed_dns</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_spoofed_gateway</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_spoofed_subnet</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>web_dos_attack</code></td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center">○✓</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
</tbody>
</table>

When you add, remove, or retarget cases (new failure, new scenario, or a `TAGS` / `COMPATIBLE_COLUMNS` / registry change that changes compatibility), refresh this section in the same change.

## Regeneration

Regenerate the candidate catalog after you add, remove, or retarget a failure, environment, or compatibility rule:

```shell
nika benchmark generate
```

Add `--validated-only` to keep IOS XR, RouterOS, and SONiC rows only for router platform cells that validation has recorded as working. Then refresh the coverage tables:

```shell
uv run python scripts/render_coverage_matrix.py --write-docs
```

Generation prints counts of groups, concrete options, failures, scenarios, healthy cases, and rejections. It does not select cases or create release splits. Frozen releases ship under `benchmark/releases/`.

### Prepare Dev and Test splits

Before [freezing a release](../operations/cli-reference.md#nika-benchmark), use the same failure set in Dev and Test, and keep task definitions disjoint. Multi-fault tasks contribute each individual failure to the failure-set comparison. Network isolation depends on the environment:

| Environment | Required separation between Dev and Test |
| --- | --- |
| ISP environments | Different backbone graphs. Named protocol variants of the same graph still share its identity. |
| Fixed-size `k8s_lab` and `llmd_lab` clusters | Different faulted components, using resolved `root_causes` and canonical resource identity independently of fault type. This replaces network-variant isolation for these clusters; healthy cluster tasks are excluded from component comparison. |
| Other environments | Different deployment variants, even when faults differ. Variant identity includes environment, scale, routing options, backend, and router profile; omitted defaults are resolved before comparison. |

Published releases retain their original split rules. Passing the new candidate gate does not resample or modify an existing release.

---

## Reference

This part holds file formats, router platform rules, capability tags, and run limits. Read it when a task file, a coverage cell, or a run outcome needs explaining.

Implementation: [`benchmark/`](../../benchmark/) stores matrices and releases; [`workflows/benchmark/`](../../src/nika/workflows/benchmark/) loads and executes them.

### Pool file format

Pool files live at `benchmark/working/pool/{scenario}/{fault_type}.yaml`, with `healthy.yaml` for the healthy cases of each scenario. The loader scans the directory, so there is no manifest file.

A failure file lists one `cases` entry per task. Each entry sets the deployment (`topo_size`, and optionally `telemetry`, `traffic`, and the ISP options `igp`, `bgp_mode`, and `backend`, or a `device_profile`), the injection parameters in `inject`, and the expected `root_causes`:

```yaml
scenario:
  name: campus_lan
failure:
  fault_type: link_down
  cases:
    - topo_size: s
      inject:
        host_name: backend_web_0
        intf_name: eth0
      root_causes:
        - resource:
            kind: link
            name: backend_web_0:eth0--backend_web_1:eth0--backend_web_2:eth0--load_balancer:eth1
          fault_type: link_down
```

A healthy file lists the deployment variants, with no injection:

```yaml
scenario:
  name: campus_lan
healthy:
  variants:
    - topo_size: s
    - topo_size: m
```

Scenarios meant for tests only, such as `iosxr_simple_bgp`, are excluded from generation.

### Router platform profiles

Some environments run routers on a platform other than FRR:

- IOS XR (`device_profile: iosxr`) and RouterOS (`device_profile: routeros`) apply to `dc_clos`, `campus_lan`, `enterprise_branch`, and the base ISP scenarios. Generation adds them only at size `s` (topology tier `s` for ISP scenarios), so no case needs a large native lab.
- SONiC (`device_profile: sonic`) applies to Containerlab environments: `min3clos` and the base ISP scenarios within the [Containerlab ISP size cap](../operations/network-scenarios.md#sndlib-isp-scenarios). SR Linux is the Containerlab default.

Each profile gets its own healthy variant. SONiC rows include only failures that the deployed environment and protocol support, with an eligible target and the required router operations.

```yaml
scenario:
  name: dc_clos
failure:
  fault_type: bgp_asn_misconfig
  cases:
    - topo_size: s
      device_profile: iosxr
      inject:
        host_name: spine_1
      root_causes:
        - resource:
            kind: node
            node: spine_1
          fault_type: bgp_asn_misconfig
```

A case targets a native router only when the failure can run there. A failure lists the router operations it needs in `native_router_operations`, and each vendor provider implements a set of them (see [Create benchmark tasks](../development/creating-benchmark-tasks.md#write-an-injectable-failure)). Other targets stay on FRR and Linux nodes. In `dc_clos`, `campus_lan`, and `enterprise_branch`, only some router roles convert, so many cases on these profiles target FRR routers and hosts in a mixed-vendor lab. Ground truth names NIKA nodes and Linux interface names, as it does for FRR rows.

A case also needs a backend that both the failure and the environment support. For a failure with a minimum path bandwidth-delay product (BDP), generation checks any declared round-trip time and profile bandwidth limit. The RouterOS CHR profile uses the [free license's 1 Mbps per-interface limit](https://help.mikrotik.com/docs/spaces/ROS/pages/18350234/Cloud%2BHosted%2BRouter%2BCHR), so the enterprise WAN's 80 ms RTT cannot supply the 64 KiB BDP that `tcp_receive_window_limited` requires. The pool excludes that deployment. Runtime calibration still checks the actual flow before it changes receiver buffers.

Rows that need licensed images are installed as follows:

- IOS XR and RouterOS rows need licensed vendor images, installed with `./scripts/install.sh --with-vendor-images`.
- SONiC rows need no license. `nika images prepare` downloads the SONiC image.
- Rows of all three platforms run as heavy trials, one at a time.

Before a run starts, `nika benchmark run --config` and `nika case run` check the licensed images their rows need, and stop with a list of any that are missing. `nika benchmark select` and releases leave out every row that needs a licensed image.

`--device-profile` is repeatable and matches the platform a row runs on. A row's own `device_profile` takes precedence. Otherwise, Containerlab rows count as `nokia_srlinux`, and other rows count as the one native platform their environment declares, with `frr` as the fallback. For example, `routeros_simple_bgp` rows count as `routeros` even without the field.

`nika benchmark validate-profiles` runs one case for each IOS XR, RouterOS, or SONiC cell end to end and records the result in `benchmark/working/validation/device_profiles.yaml`. It is heavy, so run it on demand or nightly. Recorded cells are skipped, so a rerun resumes. The [coverage matrix](#coverage-matrix-scenario--failure) shows each recorded result next to its cell, and [validate device-profile cells](../development/creating-benchmark-tasks.md#reference-validate-device-profile-cells) describes the workflow.

### Capability tags

Environments and failures declare capability tags. A failure can run on an environment when the environment has every tag the failure requires, and the failure's `COMPATIBLE_COLUMNS`, when it sets one, admits that environment and profile. Generation then checks the injection targets, so a tag match alone does not add a task.

#### Tags by environment

| Environment | Tags |
| --- | --- |
| `campus_lan` | `arp`, `dhcp`, `dns`, `forwarding_device`, `frr`, `http`, `icmp`, `link`, `load_balancer`, `mac`, `ospf`, `pc`, `web` |
| `dc_clos` | `arp`, `bgp`, `dc_clos`, `dns`, `forwarding_device`, `frr`, `http`, `icmp`, `link`, `mac`, `pc` |
| `enterprise_branch` | `arp`, `bgp`, `forwarding_device`, `frr`, `http`, `icmp`, `link`, `mac`, `nat`, `pc`, `vpn` |
| `isp_*` (base SNDlib) | `bgp`, `containerlab`, `frr`, `icmp`, `igp`, `isis`, `isp`, `link`, `ospf`, `sndlib`, `srl` |
| `isp_abilene_ebgp_rpki` / `isp_geant_ebgp_rpki` | `bgp`, `ebgp`, `frr`, `icmp`, `igp`, `isp`, `link`, `ospf`, `rpki`, `sndlib` |
| `isp_abilene_ebgp_rtbh` / `isp_dfn-bwin_ebgp_rtbh` | `bgp`, `ebgp`, `frr`, `icmp`, `igp`, `isp`, `link`, `ospf`, `rtbh`, `sndlib` |
| `k8s_lab` | `arp`, `bgp`, `coredns`, `fat-tree`, `frr`, `icmp`, `ingress`, `k3s`, `k8s_control_plane`, `k8s_storage`, `k8s_workload`, `kube_proxy`, `kubernetes`, `link`, `mac`, `metallb`, `network_policy`, `pc` |
| `llmd_lab` | `arp`, `coredns`, `http`, `icmp`, `inference`, `k3s`, `k8s_control_plane`, `kube_proxy`, `kubernetes`, `link`, `llm`, `mac`, `metallb`, `network_policy`, `pc` |
| `min3clos` | `arp`, `bgp`, `clos`, `containerlab`, `fabric`, `forwarding_device`, `link`, `srl` |
| `p4_dc_fabric` | `arp`, `http`, `icmp`, `link`, `mac`, `p4`, `p4_runtime`, `pc` |
| `p4_dc_gateway` | `arp`, `ecn`, `flow_tracking`, `http`, `icmp`, `int`, `l4_load_balancer`, `link`, `mac`, `p4`, `p4_runtime`, `pc`, `queue`, `telemetry` |
| `sdn_l3_clos` | `arp`, `forwarding_device`, `http`, `icmp`, `link`, `mac`, `pc`, `sdn` |

#### Tag meanings

| Tag | Meaning |
| --- | --- |
| `arp` | ARP / L2 neighbor resolution present |
| `bgp` | BGP routing (FRR or equivalent) |
| `clos` | Clos / leaf-spine style fabric |
| `containerlab` | Containerlab backend (not Kathara) |
| `coredns` | Kubernetes CoreDNS service |
| `dc_clos` | The `dc_clos` environment itself |
| `dhcp` | DHCP server / clients in the lab |
| `dns` | DNS server / resolver path |
| `ebgp` | eBGP sessions between autonomous systems |
| `ecn` | Per-port ECN marking configuration |
| `fabric` | Multi-switch fabric topology |
| `fat-tree` | Fat-tree underlay (k8s lab) |
| `flow_tracking` | Flow tracking on the P4 gateway |
| `forwarding_device` | Routers or switches that device-level failures can target |
| `frr` | FRRouting daemons on routers |
| `http` | HTTP / web service endpoints |
| `icmp` | ICMP reachability usable for diagnosis |
| `igp` | Interior gateway protocol (OSPF or IS-IS) in the ISP backbone |
| `inference` | LLM inference workload (llmd) |
| `ingress` | Kubernetes ingress path |
| `int` | P4 In-band Network Telemetry |
| `isis` | IS-IS intradomain routing |
| `isp` | ISP backbone environment built from an SNDlib graph |
| `k3s` | Lightweight Kubernetes (k3s) |
| `k8s_control_plane` | Kubernetes control-plane access |
| `k8s_storage` | Kubernetes storage workloads |
| `k8s_workload` | Kubernetes application workloads |
| `kube_proxy` | Kubernetes Service routing via kube-proxy |
| `kubernetes` | Kubernetes control/data plane |
| `l4_load_balancer` | Layer-4 load balancer on the P4 gateway |
| `link` | Controllable L2/L3 links (down, flap, QoS, …) |
| `llm` | LLM-serving scenario features |
| `load_balancer` | Load-balancer node/service |
| `mac` | MAC addressing / L2 identity |
| `metallb` | MetalLB service advertisement |
| `mpls` | P4 MPLS label stack |
| `nat` | Source NAT at the enterprise edge |
| `network_policy` | Kubernetes NetworkPolicy enforcement |
| `ospf` | OSPF intradomain routing |
| `p4` | BMv2 / P4 switches |
| `p4_runtime` | P4Runtime southbound (`simple_switch_grpc`, ActionSelector) |
| `pc` | End hosts (PCs) that can be misconfigured |
| `queue` | Per-port queueing on the P4 gateway |
| `rpki` | RPKI origin validation (Routinator RTR and VRP-based ROV) |
| `rtbh` | Remotely triggered black hole (RTBH) routing |
| `sdn` | SDN controller + OpenFlow switches |
| `sndlib` | Topology and demand matrices imported from SNDlib |
| `srl` | Nokia SR Linux NOS, the default Containerlab router platform; `device_profile: sonic` runs SONiC instead |
| `telemetry` | Telemetry export: flow records, device metrics, or syslog; see [telemetry](../operations/network-scenarios.md#telemetry) |
| `vpn` | VPN membership / tunnels |
| `web` | Web-tier services (alias capability for HTTP labs) |

### Run limits and retries

- **Agent budget.** `agent.timeout_sec` (default 1800 seconds) limits each agent run for every agent type. Keep it below `benchmark.case_timeout_sec`.
- **Case watchdog.** `--case-timeout` or `benchmark.case_timeout_sec` (default 2400 seconds) stops a trial. A timed-out trial becomes `agent_failed` when the time went to agent work. It becomes `endpoint_failed` when the kill landed during a model request that took most of the wall clock. NIKA sends SIGTERM to the worker and waits up to 60 seconds for it to undeploy the lab before it sends SIGKILL.
- **Outcome classification.** A failure counts as `agent_failed` only when the agent started and made at least one model or tool call. A valid `submission.json` makes the trial `success`, even when a later step such as an agent timeout or sandbox teardown fails, provided the scheduler finished without errors where it reports them.
- **Endpoint breaker.** After `benchmark.endpoint_failure_threshold` consecutive `endpoint_failed` trials (default 3), NIKA stops starting trials. It probes the model with an interval that starts at `endpoint_backoff_initial_sec` and doubles up to `endpoint_backoff_max_sec`. Running trials can finish meanwhile. A successful probe resumes starts. A `success` or `agent_failed` outcome resets the count. An `infra_failed` outcome does not change it. If the endpoint stays down past `endpoint_wait_timeout_sec` (default 900 seconds), NIKA aborts the running trials and leaves them for `--resume`.
- **Probe cost.** Probes use the agent's API protocol, endpoint, and key, and each sends a short request. They can incur provider charges.
- **Continuing past failures.** Release runs continue past failed trials by default. Ad-hoc batches stop at the first failure unless `benchmark.continue_on_error` is set. `--retry-passes` re-scans failed or incomplete trials after the first pass, and it also continues past failures.
- **Batch sizes.** `benchmark.batch_size` sets how many light trials run at once. `benchmark.heavy_batch_size` sets how many heavy trials run at once (Containerlab, Kubernetes, XRd, and size `l`). Heavy and light trials never overlap.
- **Identity.** Each trial's `run.json` repeats the release identity and adds `trial_id`, `trial_index`, and `outcome`.

### Published releases

Frozen releases keep their published flat `cases:` files. Generation does not change them. Release `0.1.0` includes its original environments, listed in [Where each case runs](../compat/release-0.1.0.md#where-each-case-runs).

Outside the published releases, `frr_service_down` resolves to `routing_service_down`. Releases `0.1.0` and `0.2.0` keep `frr_service_down` in their tasks, ground truth, and root-cause candidates, with its original description.

