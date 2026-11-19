# Benchmark configuration reference

This reference helps benchmark operators choose a benchmark release or a working task set and find the resulting artifacts.

Implementation: [`benchmark/`](../../benchmark/) stores matrices and releases; [`workflows/benchmark/`](../../src/nika/workflows/benchmark/) loads and executes them.

Use a task-level `schedule` to control failure injection, recovery, traffic activation, and agent startup. [Incident scheduling](scheduling.md) describes the YAML, timing, and completion rules.

For ad-hoc runs, use a benchmark YAML whose `cases:` list contains relative paths to complete task files, or run one task file directly with `--config`. Each task file defines its scenario, failures, traffic, and schedule. Existing matrices containing only inline tasks remain supported. [Organize task files](scheduling.md#organize-task-files) shows the standard layout.

## Benchmark release (`nika-bench@0.3.0`)

`0.3.0` is provisional until its release tag exists. Its task files can be inspected and audited locally. The release workflow below requires the tagged benchmark engine.

A benchmark release provides public Dev and Test splits. Labels are public for reproducibility. Each version directory under [`benchmark/releases/`](../../benchmark/releases/) includes a `README.md` with suite shape, scenario and failure coverage, and links to the scenario and failure references.

```shell
nika benchmark releases
nika benchmark run --release 0.3.0 --split test --result_dir results/my-run
```

Release runs select the **main set** by default: core, healthy, and multi-fault tasks. The leaderboard ranks the Test main score. To measure vendor and size comparisons, run a representative agent with the optional slices:

```shell
nika benchmark run --release 0.3.0 --split test --include-slices --result_dir results/my-slices
```

`--include-slices` adds vendor twins and third-size tasks available in the selected split. The equivalent run setting is `benchmark.include_slices: true`; `--main-only` overrides it. Use a separate results directory when changing selection. The optional rows never enter the [main score](root-cause-evaluation.md#aggregate-leaderboard-and-nika-eval-summary), and most leaderboard entrants only need the main set.

Release `0.3.0` runs on its [benchmark engine](../development/benchmark-contracts.md#b-lifecycle-protocol), a container built from the Git reference in `engine.tag` of its `RELEASE.yaml`, so its environments, failures, MCP tools, and scoring stay those of the release as NIKA changes. The first run builds the engine image; `nika engine build <tag>` builds it ahead of time. Firecracker execution and NIKA Remote cannot run it yet.

The [versioning policy](../../VERSIONING.md) explains what each component of a release version means and which changes produce a new one.

Each release run treats `--result_dir` as **one run** and writes:

| Path | Role |
|------|------|
| `run.json` (and legacy `benchmark_job.json`) | Durable run config: release version/`split`, agent/model/`n_trials`, timeout, `official`, stable `run_id` |
| `RELEASE.lock.json` | Slim identity lock |
| `trials/{task_id}__tNN/` | One counted trial. `task_id` is the public case id (same string as filesystem `case_key`). List and describe cases with `nika benchmark list` / `describe`; re-run one trial with `nika benchmark run --task-id`. See the [CLI reference](../operations/cli-reference.md#task-ids). |

Active run progress is recorded under `runtime/benchmark_runs/{run_id}.json`.

After a finished release run, submit a leaderboard entry (packs and validates automatically). See the [leaderboard submission guide](leaderboard-submission.md).

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard submit --result_dir results/my-run --submission results/my-run/submission
```

`defaults.n_trials` in `RELEASE.yaml` is 3 for `0.3.0`. It expands the selected task set to `case_count × n_trials` deterministic trials. Resume skips completed trials, including `outcome=agent_failed` (capability / no submission / tool-heavy or post-LLM case timeout). LLM/API endpoint failures (connection refused, provider 5xx, case-timeout killed mid-LLM after LLM-dominated wall clock, etc.) stamp `outcome=endpoint_failed` instead: they are **not** scored and `--resume` cleans and retries them. This is about the model HTTP endpoint, not the network lab under test. Failures after ground truth is written but before the agent demonstrably started (sandbox CLI missing, MCP gateway URL or bind failure, a worker killed during agent setup) stamp `outcome=infra_failed`. NIKA treats them like `endpoint_failed`: not scored, cleaned, and retried. NIKA counts a failure as `agent_failed` only when `nika.jsonl` has `agent_start` and `messages.jsonl` has at least one model or tool event. A valid `submission.json` makes the trial `success`, even when a later step (agent timeout, sandbox teardown) fails, provided its scheduler completed without errors when present. NIKA also cleans incomplete trials in place, so retries stay within K. Different `--result_dir` values create isolated runs and do not skip each other's trials.

Official `--release` runs default to continuing past trial failures (`continue_on_error=True`) so one bad trial does not abort the job; use `--abort-on-error` to stop immediately. Ad-hoc `--config` batches still default from `benchmark.continue_on_error` in run config. The agent itself runs under `agent.timeout_sec` (default 1800s), which applies to every agent type. Per-case watchdogs use `--case-timeout` / `benchmark.case_timeout_sec` (default 2400s); a timed-out trial is finalized as counted `agent_failed` when the budget was spent on agent work, or as retryable `endpoint_failed` when the kill lands mid-LLM and LLM calls dominated the wall clock. NIKA stops a timed-out worker with SIGTERM and gives it up to 60 seconds to remove its sandbox, stop the MCP gateway, and undeploy the lab before SIGKILL.

After `benchmark.endpoint_failure_threshold` consecutive `endpoint_failed` trials (default 3), NIKA pauses new trials and starts the recovery wait limit. Running trials can finish while NIKA probes the configured model, starting after `endpoint_backoff_initial_sec` (5 seconds) and doubling the interval up to `endpoint_backoff_max_sec` (60 seconds). A successful probe resumes admission. A counted `success` or `agent_failed` outcome resets the failure counter; `infra_failed` does not change it. After `endpoint_wait_timeout_sec` (900 seconds), NIKA aborts running trials and leaves incomplete trials available for `--resume`.

Recovery probes use the agent's API protocol, upstream endpoint, and API key or subscription login. They request a short response and can incur provider charges. Each probe has a time limit within the remaining recovery wait.

Per-trial `run.json` is stamped with the same release identity fields plus `trial_id` / `trial_index` / `outcome`.

## Working matrix

`benchmark/working/` is the in-development matrix. It has two parts: the full executable pool and a coverage-selected runnable subset.

`benchmark/working/pool/` is the executable case space derived from the live problem and scenario registries. Each YAML under that directory is one failure group or healthy baseline for a specific scenario. Failure files list one flat executable row per `cases` entry. Healthy candidates contain one deployable baseline per variant and no injection.

Generate or refresh the pool, then run it as the default config or an explicit path:

```bash
nika benchmark generate
nika benchmark run
nika benchmark run --config benchmark/working/pool
```

The runner loads each catalog case as an executable row before creating trials. It recomputes ground truth during injection and checks it against the materialized `root_causes`. `candidate_option_id` is derived at load time from the normalized row; resume and release identity use `benchmark_row_fingerprint` (canonical scenario, problem, deployment, injection, traffic, and authored schedule fields).

### Pool schema

Pool files live at `working/pool/{scenario}/{fault_type}.yaml` (plus `healthy.yaml`). There is no separate manifest file; loaders scan the pool directory.

Failure group files use `scenario` → `failure` → `cases`. Each task carries `topo_size`, all supported [`telemetry`](../operations/network-scenarios.md#telemetry) kinds, optional [`traffic`](../operations/network-scenarios.md#background-traffic-profiles), scalar `inject`, and materialized `root_causes`. Base ISP tasks also carry `igp`, `bgp_mode`, `backend`, and `device_profile`. Other environments use `device_profile` when selecting router platforms. A row without `device_profile` deploys the environment's default routers.

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
    - topo_size: s
      inject:
        host_name: dhcp_server
        intf_name: eth0
      root_causes:
        - resource:
            kind: link
            name: dhcp_server:eth0--server_access_router:eth6
          fault_type: link_down
```

Healthy files use one `healthy.yaml` per scenario with deploy variants:

```yaml
scenario:
  name: campus_lan
healthy:
  variants:
    - topo_size: s
    - topo_size: m
```

### Router platform profiles

The pool also carries cases whose routers run IOS XR (`device_profile: iosxr`) or RouterOS (`device_profile: routeros`). The generator expands each environment that declares these profiles: `dc_clos`, `campus_lan`, `enterprise_branch` and the base ISP scenarios. It adds them only at size `s` (for ISP scenarios, topology tier `s`), within the sizes the environment supports, so no case needs a large native lab. Each profile gets its own healthy variant.

Containerlab deployments run SR Linux by default and also carry SONiC rows (`device_profile: sonic`): `dc_clos` at every supported size and the base ISP scenarios within the [Containerlab ISP size cap](../operations/network-scenarios.md#sndlib-isp-scenarios) (11 SR Linux routers or 55 SONiC routers). SONiC rows include only failures supported by the deployed environment and protocol, with an eligible target and the required router operations.

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

A case targets a native router only when the failure can run there. A failure declares the native router operations it needs in `native_router_operations`, and each vendor provider implements a set of operations (see [Create benchmark tasks](../development/creating-benchmark-tasks.md#add-an-injectable-failure)). Other targets stay on FRR and Linux nodes. In `dc_clos`, `campus_lan` and `enterprise_branch` only some router roles convert, so many cases of these profiles target FRR routers and hosts in a mixed-vendor lab. Ground truth names NIKA nodes and Linux interface names, as for FRR rows.

Compatibility also requires a backend supported by both the failure and the environment. For failures with a minimum path bandwidth-delay product (BDP), generation checks any declared nominal RTT and profile bandwidth limit. The shipped RouterOS CHR profile uses the [free license's 1 Mbps per-interface limit](https://help.mikrotik.com/docs/spaces/ROS/pages/18350234/Cloud%2BHosted%2BRouter%2BCHR), so the enterprise WAN's designed 80 ms RTT cannot provide the 64 KiB BDP required by `tcp_receive_window_limited`. The pool excludes that deployment; runtime calibration still checks the actual flow before changing receiver buffers. Generation and case validation exclude `endpoint_cpu_contention` on profiles capped below 10 Mbit/s. The 1 Mbit/s path can mask the required fivefold bulk-transfer slowdown even when the CPU artifact is present.

`ProblemBase.is_compatible(scenario, device_profile)` and `runs_on_device_profile` record this compatibility; the coverage matrix below shows it per environment and profile, with the last result of `nika benchmark validate-profiles` (see [Validate device-profile cells](../development/creating-benchmark-tasks.md#validate-device-profile-cells)). `nika benchmark generate --validated-only` keeps IOS XR, RouterOS and SONiC rows only for validated cells.

IOS XR and RouterOS rows need licensed vendor images (`./scripts/install.sh --with-vendor-images`). SONiC rows need no license: `uv run nika images prepare` downloads the SONiC image. Rows of all three platforms run as heavy trials, one at a time. The release covers all five router platforms across its main set and vendor twins; each split's main set need not contain every platform. A full run requires user-prepared XRd and RouterOS images; see [Prepare vendor images](../operations/installation.md#prepare-vendor-images).

Before any lab starts, `nika benchmark run --config` and `nika case run` check that the licensed images their rows need are installed. When one is missing, the run stops and lists the image and the affected rows by scenario and platform. Choose which rows a pool run includes:

```shell
nika benchmark run --config benchmark/working/pool --skip-licensed            # local debugging subset
nika benchmark run --config benchmark/working/pool --device-profile routeros  # RouterOS rows only
```

`--device-profile` is repeatable. An explicit `device_profile` takes precedence. Otherwise Containerlab rows count as `nokia_srlinux` and Kathara rows as `frr`.

Paths follow `working/pool/{scenario}/{fault_type}.yaml`, plus `working/pool/{scenario}/healthy.yaml`. Scenarios marked for tests only are excluded from generation.

### Selected cases

The pool is the full executable space. A coverage-guided selector writes a compact subset to `benchmark/working/cases.yaml` (selection runs the pool audit gate unless `--skip-audit` is set):

```bash
nika benchmark select
```

| Artifact | Role |
|----------|------|
| `benchmark/working/cases.yaml` | Selected flat `cases:` matrix |

Run the selected matrix with:

```bash
nika benchmark run --config benchmark/working/cases.yaml
```

## Tags

Scenarios and failures declare capability `TAGS`. A failure may run on a scenario when every problem tag is present on the scenario (`problem.TAGS ⊆ scenario.TAGS`) and `COMPATIBLE_COLUMNS`, when set, admits that scenario/profile. The generator then applies semantic target enumeration and case validation. Tag compatibility alone does not put an invalid target in the catalog.

### Tag meanings

| Tag | Meaning |
|-----|---------|
| `arp` | ARP / L2 neighbor resolution present |
| `bgp` | BGP routing (FRR or equivalent) |
| `coredns` | Kubernetes CoreDNS service |
| `clos` | Clos / leaf-spine style fabric |
| `containerlab` | Containerlab backend (not Kathara) |
| `dhcp` | DHCP server / clients in the lab |
| `dns` | DNS server / resolver path |
| `fabric` | Multi-switch fabric topology |
| `fat-tree` | Fat-tree underlay (k8s lab) |
| `frr` | FRRouting daemons on routers |
| `http` | HTTP / web service endpoints |
| `icmp` | ICMP reachability usable for diagnosis |
| `ingress` | Kubernetes ingress path |
| `inference` | LLM inference workload (llmd) |
| `int` | P4 In-band Network Telemetry |
| `k3s` | Lightweight Kubernetes (k3s) |
| `k8s_control_plane` | Kubernetes control-plane access |
| `k8s_storage` | Kubernetes storage workloads |
| `k8s_workload` | Kubernetes application workloads |
| `kube_proxy` | Kubernetes Service routing via kube-proxy |
| `kubernetes` | Kubernetes control/data plane |
| `link` | Controllable L2/L3 links (down, flap, QoS, …) |
| `llm` | LLM-serving scenario features |
| `load_balancer` | Load-balancer node/service |
| `mac` | MAC addressing / L2 identity |
| `metallb` | MetalLB service advertisement |
| `mpls` | P4 MPLS label stack |
| `network_policy` | Kubernetes NetworkPolicy enforcement |
| `ospf` | OSPF intradomain routing |
| `p4` | BMv2 / P4 switches |
| `p4_runtime` | P4Runtime southbound (`simple_switch_grpc`, ActionSelector) |
| `pc` | End hosts (PCs) that can be misconfigured |
| `rpki` | RPKI origin validation (Routinator RTR and VRP-based ROV) |
| `sdn` | SDN controller + OpenFlow switches |
| `srl` | Nokia SR Linux NOS, the default Containerlab router platform; `device_profile: sonic` runs SONiC instead |
| `vpn` | VPN membership / tunnels |
| `web` | Web-tier services (alias capability for HTTP labs) |

### Scenario tags

| Scenario | Tags |
|----------|------|
| `campus_lan` | `arp`, `dhcp`, `dns`, `frr`, `http`, `icmp`, `link`, `load_balancer`, `mac`, `ospf`, `pc`, `web` |
| `dc_clos` | `arp`, `bgp`, `dns`, `frr`, `http`, `icmp`, `link`, `mac`, `pc` |
| `enterprise_branch` | `arp`, `bgp`, `frr`, `http`, `icmp`, `link`, `mac`, `pc`, `vpn` |
| `isp_*` (base SNDlib) | `bgp`, `containerlab`, `frr`, `icmp`, `igp`, `isis`, `isp`, `link`, `ospf`, `sndlib`, `srl` |
| `k8s_lab` | `arp`, `bgp`, `coredns`, `fat-tree`, `frr`, `icmp`, `ingress`, `k3s`, `k8s_control_plane`, `k8s_storage`, `k8s_workload`, `kube_proxy`, `kubernetes`, `link`, `mac`, `metallb`, `network_policy`, `pc` |
| `llmd_lab` | `arp`, `coredns`, `http`, `icmp`, `inference`, `k3s`, `k8s_control_plane`, `kube_proxy`, `kubernetes`, `link`, `llm`, `mac`, `metallb`, `network_policy`, `pc` |
| `p4_dc_fabric` | `arp`, `http`, `icmp`, `link`, `mac`, `p4`, `p4_runtime`, `pc` |
| `p4_dc_gateway` | `arp`, `ecn`, `flow_tracking`, `http`, `icmp`, `int`, `link`, `mac`, `p4`, `p4_runtime`, `pc`, `queue`, `telemetry` |
| `sdn_l3_clos` | `arp`, `http`, `icmp`, `link`, `mac`, `pc`, `sdn` |

## Generation statistics

The generator prints group, concrete option, failure, scenario, healthy, and rejection counts to stdout.

## Published releases

Frozen releases keep their published flat `cases:` files and remain independent from candidate generation. See the [versioning policy](../../VERSIONING.md) to reproduce an older release.

Configurations and task labels accept `frr_service_down` as an alias for `routing_service_down`. Release `0.3.0` uses `routing_service_down` in its tasks, ground truth, and RCA candidates.

## Coverage matrix (scenario × failure)

Cells mark **capability**. A failure is compatible when its tags and deploy constraints match the scenario and ISP `topo`/`igp`/`bgp_mode` profile. Candidate generation then validates semantic inject targets. Release membership comes from `benchmark/releases/0.3.0/` (dev + test).

Each table has two header rows: scenario, then config (when the scenario has more than one). Cells: blank = incompatible, `○` = compatible, `●` = compatible and in release `0.3.0`. Tables are split by failure subsystem.

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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
</tr>
<tr>
<td><code>link_down</code></td>
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
</tr>
<tr>
<td><code>link_flap</code></td>
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
</tr>
<tr>
<td><code>link_latency</code></td>
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
<td><code>link_packet_corruption</code></td>
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
<td align="center">●</td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
</tr>
<tr>
<td><code>bgp_asn_misconfig</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>bgp_blackhole_community_leak</code></td>
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
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_max_prefix_limit_low</code></td>
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
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_md5_password_mismatch</code></td>
<td align="center"></td>
<td align="center">●</td>
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
</tr>
<tr>
<td><code>bgp_missing_route_advertisement</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>bgp_prefix_list_length_mismatch</code></td>
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
</tr>
<tr>
<td><code>bgp_prefix_surge</code></td>
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
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_route_leak</code></td>
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
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_rov_not_enforced</code></td>
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
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
</tr>
<tr>
<td><code>bgp_ttl_security_mismatch</code></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>isis_authentication_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
</tr>
<tr>
<td><code>isis_metric_style_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>isis_router_level_mismatch</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
</tr>
<tr>
<td><code>ospf_area_misconfiguration</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>ospf_area_range_suppression</code></td>
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
<td><code>ospf_duplicate_router_id</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>ospf_hello_interval_mismatch</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>ospf_interface_not_enabled</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>ospf_network_type_mismatch</code></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>ospf_stub_area_mismatch</code></td>
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
<td><code>routing_service_down</code></td>
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
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>vrf_route_target_mismatch</code></td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>bgp_acl_block</code></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
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
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
</tr>
<tr>
<td><code>device_flow_blackhole</code></td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>device_forwarding_packet_corruption</code></td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>dns_port_blocked</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">●</td>
</tr>
<tr>
<td><code>host_acl_block</code></td>
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
<td><code>host_static_blackhole</code></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
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
</tr>
<tr>
<td><code>icmp_frag_needed_filter_misconfiguration</code></td>
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>mtu_mismatch</code></td>
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>ospf_acl_block</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center"></td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center">●</td>
<td align="center"></td>
</tr>
<tr>
<td><code>rib_fib_inconsistency</code></td>
<td align="center">●</td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
<td align="center">●</td>
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
<td align="center">●</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>k8s_node_firewall_drop</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">●</td>
<td align="center">○</td>
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
<td align="center">●</td>
<td align="center">○</td>
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
<td align="center">●</td>
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
<td align="center">●</td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
<td align="center">●</td>
<td align="center">○</td>
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
<td align="center">●</td>
<td align="center">○</td>
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
<td align="center">●</td>
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
<td align="center">●</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">●</td>
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
<td align="center">●</td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
</tr>
<tr>
<td><code>dhcp_option_dns</code></td>
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
<td><code>dhcp_option_gateway</code></td>
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
<td><code>dhcp_option_subnet</code></td>
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
</tr>
<tr>
<td><code>dns_record_error</code></td>
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
</tr>
<tr>
<td><code>dns_service_down</code></td>
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
</tr>
<tr>
<td><code>host_incorrect_dns</code></td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_ip</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>host_ip_conflict</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>host_missing_ip</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>mac_address_conflict</code></td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
<td><code>endpoint_cpu_contention</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
<td><code>overlay_qos_ef_undersized</code></td>
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
</tr>
<tr>
<td><code>p4_ecn_marking_disabled</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
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
<th colspan="4">isp_abilene</th>
<th colspan="4">isp_france</th>
<th colspan="4">isp_pioro40</th>
<th rowspan="2">k8s</th>
<th rowspan="2">llmd</th>
<th rowspan="2">p4_dc_fabric</th>
<th rowspan="2">p4_dc_gateway</th>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>bgp_hijacking</code></td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
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
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
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
<td><code>link_latency</code></td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center"></td>
<td align="center">◆</td>
<td align="center"></td>
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
<td><code>bgp_max_prefix_limit_low</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
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
<td><code>bgp_prefix_surge</code></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
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
<td><code>isis_router_level_mismatch</code></td>
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
<td><code>ospf_interface_not_enabled</code></td>
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
<td><code>host_acl_block</code></td>
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
<td><code>dhcp_option_dns</code></td>
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
<td><code>dhcp_option_gateway</code></td>
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
<td><code>dhcp_option_subnet</code></td>
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
<td><code>endpoint_cpu_contention</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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

Regenerate the candidate catalog:

```shell
nika benchmark generate
```

Then refresh the capability coverage tables:

```shell
uv run python scripts/render_coverage_matrix.py --write-docs
```

Candidate generation does not select cases or create release splits. Benchmark releases ship under `benchmark/releases/`.

### Prepare Dev and Test splits

Generate a candidate split before [freezing a release](../operations/cli-reference.md#nika-benchmark):

```shell
uv run nika benchmark split --pool benchmark/working/pool --output /tmp/nika-splits --seed 42
```

The generator jointly chooses network ownership, core tasks, vendor twins, and third-size tasks. Every fault has one Dev and one Test core task in the same family at different sizes. Non-ISP cores also use the same environment. Backend, platform, protocol, traffic, telemetry, and objectives match across the pair. Core tasks use FRR or the default platform where possible.

The split excludes tasks whose Firecracker VM memory exceeds `--max-vm-memory-gib` (default 38 GiB), including from the Dev training pool. The report and release metadata record this limit.

Among faults with candidates at all three sizes in their selected core family, each of s/m, s/l, and m/l must account for at least 25% of the core pairs. The joint solve enforces this share alongside network ownership and the other quotas. Faults whose selected family lacks a size are exempt from the share constraint.

`bgp_address_family_not_activated` and `bgp_session_policy_missing` only have size-s candidates. Their cores use different small ISP graphs and carry `size_comparison_excluded: true`. They still enter the main score.

Vendor twins reuse an existing FRR core through `base_task_id`. Only the vendor row is added. A twin preserves the network, environment, fault, full injection parameters, protocol, traffic, telemetry, and objectives; platform and backend may differ. One core can anchor several vendors. Each vendor/fault comparison appears once, on its base's split. SR Linux supports the declared m/l Clos deployments as well as s; IOS XR and RouterOS candidates are s-only.

Rows carry `vendor_tier: A` for matched ISP-backbone comparisons or `vendor_tier: C` for the remaining vendor comparisons. Tier A prioritizes ten eligible ISP-backbone faults on dfn-bwin, dfn-gwin, di-yuan, or pdh, with all four vendors sharing each FRR base.

The core rule permits one small graph per fault. A tier-A cell requires support from all four vendors and a different-size ISP core pair. `bgp_md5_password_mismatch` has no ISP m/l candidate; `bgp_acl_block` and `ospf_acl_block` lack a supported SONiC filter. These faults cannot supply complete tier-A cells. The release README records the selected faults and counts. The total vendor slice is capped at 156 twins.

Third-size tasks refer to both cores through `core_task_ids` and complete their s/m/l sizes on Dev networks. The joint solve targets two dozen such tasks, with one to six per family, balanced across failure domains. Each split has 19 healthy tasks. Every catalog multi-fault task follows its network; the target is an equal Dev/Test allocation, subordinate to hard feasibility.

The deterministic integer program first maximizes complete four-vendor tier-A cells. It then minimizes the sum of marginal TVD excess above 0.05 on the main tasks' family, failure domain, size, backend, and platform. Each task contributes unit mass; a multi-domain task shares that mass equally across its distinct failure domains. It fills the remaining vendor capacity with tier C, balances third-size domains, and minimizes heavy-task count. `report.json` records the resulting TVDs, quota fallbacks, solver bounds, and counts. A TVD miss is a soft-objective result, not permission to violate network isolation or core matching. Generation does not run admission or Oracle checks.

All device profiles, backends, and protocol variants of a network belong to the same split:

| Environment | Required separation between Dev and Test |
| --- | --- |
| ISP environments | Different backbone graphs. Protocol variants of the same graph share its identity. |
| Other environments, including `k8s_lab`, `llmd_lab`, and `dc_clos` | Different `(scenario, resolved topo_size)` identities. Omitted sizes use the registry deployment default. |

The generator treats ISP graphs as one network inventory and each other environment's sizes as its own inventory. It excludes inventories with only one network identity in the input pool and lists them in the report. Required scales are s/m/l.

`dev_pool.yaml` contains every untimed single-fault candidate, healthy candidate, and catalog multi-fault candidate on a Dev network for public training. Single-fault scheduled candidates remain deferred. Rows carry `family`, `network_id`, `topo_scale`, resolved platform and backend, `router_platform` (`none` for non-router environments), `failure_domain`, and `slice`. A multi-fault task's domain label joins its distinct failure domains. Release loading preserves these labels for selection and aggregation; ordinary ad-hoc loading strips them. The release manifest fixes the default main selection and the two optional slices. The freeze path checks training-pool isolation and records its hash.


Full release runs require every image. `--skip-licensed` selects a subset for local debugging.
