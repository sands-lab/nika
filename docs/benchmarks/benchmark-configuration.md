# Benchmark configuration reference

This reference is for maintainers and benchmark operators who need to choose a frozen release or a working YAML matrix and understand its artifact layout.

Implementation: [`benchmark/`](../../benchmark/) stores matrices and releases; [`workflows/benchmark/`](../../src/nika/workflows/benchmark/) loads and executes them.

## Frozen release (`nika-bench@0.2.0`)

`0.2.0` publishes public Dev and Test splits from the current failure registry. Both splits cover every registered failure. Labels are public for reproducibility. Each version directory under [`benchmark/releases/`](../../benchmark/releases/) includes a `README.md` with suite shape, scenario and failure coverage, and links to the scenario and failure references.

```shell
nika benchmark releases
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
```

Release `0.1.0` remains under `benchmark/releases/0.1.0/` for provenance. Its legacy IDs are not runnable, and `nika benchmark releases` reports it as `DEPRECATED` with guidance to use `--release 0.2.0`.

Each release run treats `--result_dir` as **one run** and writes:

| Path | Role |
|------|------|
| `run.json` (and legacy `benchmark_job.json`) | Durable run config: release version/`split`, agent/model/`n_trials`, timeout, `official`, stable `run_id` |
| `RELEASE.lock.json` | Slim identity lock |
| `trials/{case_key}__tNN/` | One counted trial (session artifacts) |

Active run progress is recorded under `runtime/benchmark_runs/{run_id}.json`.

After a finished release run, submit a leaderboard entry (packs and validates automatically). See the [leaderboard submission guide](leaderboard-submission.md).

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard submit --result_dir results/my-run --submission results/my-run/submission
```

`defaults.n_trials` in `RELEASE.yaml` is 3 for `0.2.0`. It expands the split to `case_count × n_trials` deterministic trials. Resume skips completed trials, including `outcome=agent_failed`. NIKA cleans and reruns incomplete trials in place, so retries stay within K. Different `--result_dir` values create isolated runs and do not skip each other's trials.

Official `--release` runs default to continuing past trial failures (`continue_on_error=True`) so one bad trial does not abort the job; use `--abort-on-error` to stop immediately. Ad-hoc `--config` batches still default from `benchmark.continue_on_error` in run config. Per-case watchdogs use `--case-timeout` / `benchmark.case_timeout_sec` (default 2400s); a timed-out trial is finalized as counted `agent_failed` when possible so resume can skip it.

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

The runner loads each catalog case as an executable row before creating trials. It recomputes ground truth during injection and checks it against the materialized `root_causes`. `candidate_option_id` is derived at load time from the normalized row; resume and release identity use `benchmark_row_fingerprint` (canonical scenario / problem / deploy / inject fields, no content hash).

### Pool schema

Pool files live at `working/pool/{scenario}/{fault_type}.yaml` (plus `healthy.yaml`). There is no separate manifest file; loaders scan the pool directory.

Failure group files use `scenario` → `failure` → `cases`. Each case carries the deploy profile (`topo_size`, and for base ISP scenarios `igp` / `bgp_mode` / `backend` / `device_profile` when applicable), scalar `inject`, and materialized `root_causes` in the same shape as frozen release cases.

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

Paths follow `working/pool/{scenario}/{fault_type}.yaml`, plus `working/pool/{scenario}/healthy.yaml`. Scenarios that are not yet validated, such as `iosxr_simple_bgp`, are excluded from generation.

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
| `srl` | Nokia SR Linux NOS (Containerlab) |
| `vpn` | VPN membership / tunnels |
| `web` | Web-tier services (alias capability for HTTP labs) |

### Scenario tags

| Scenario | Tags |
|----------|------|
| `campus_lan` | `arp`, `dhcp`, `dns`, `frr`, `http`, `icmp`, `link`, `load_balancer`, `mac`, `ospf`, `pc`, `web` |
| `dc_clos` | `arp`, `bgp`, `dns`, `frr`, `http`, `icmp`, `link`, `mac`, `pc` |
| `enterprise_branch` | `arp`, `bgp`, `frr`, `http`, `icmp`, `link`, `mac`, `pc`, `vpn` |
| `isp_*` (base SNDlib) | `bgp`, `containerlab`, `frr`, `icmp`, `igp`, `isis`, `isp`, `link`, `ospf`, `sndlib`, `srl` |
| `isp_abilene_ebgp_rpki` / `isp_geant_ebgp_rpki` | `bgp`, `ebgp`, `frr`, `icmp`, `igp`, `isp`, `link`, `ospf`, `rpki`, `sndlib` |
| `isp_abilene_ebgp_rtbh` / `isp_dfn-bwin_ebgp_rtbh` | `bgp`, `ebgp`, `frr`, `icmp`, `igp`, `isp`, `link`, `ospf`, `rtbh`, `sndlib` |
| `k8s_lab` | `arp`, `bgp`, `coredns`, `fat-tree`, `frr`, `icmp`, `ingress`, `k3s`, `k8s_control_plane`, `k8s_storage`, `k8s_workload`, `kube_proxy`, `kubernetes`, `link`, `mac`, `metallb`, `network_policy`, `pc` |
| `llmd_lab` | `arp`, `coredns`, `http`, `icmp`, `inference`, `k3s`, `k8s_control_plane`, `kube_proxy`, `kubernetes`, `link`, `llm`, `mac`, `metallb`, `network_policy`, `pc` |
| `min3clos` | `bgp`, `clos`, `containerlab`, `fabric`, `link`, `srl` |
| `p4_dc_fabric` | `arp`, `http`, `icmp`, `link`, `mac`, `p4`, `p4_runtime`, `pc` |
| `p4_dc_gateway` | `arp`, `ecn`, `flow_tracking`, `http`, `icmp`, `int`, `link`, `mac`, `p4`, `p4_runtime`, `pc`, `queue`, `telemetry` |
| `sdn_l3_clos` | `arp`, `http`, `icmp`, `link`, `mac`, `pc`, `sdn` |

## Generation statistics

The generator prints group, concrete option, failure, scenario, healthy, and rejection counts to stdout.

Release `0.1.0` is deprecated: its flat case files keep legacy scenario and failure ids, and loaders do not rewrite them. Release `0.2.0` uses the current IDs.

Frozen releases keep their published flat `cases:` files and remain independent from candidate generation.

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
<td align="center">○</td>
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
<td align="center">●</td>
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
</tr>
<tr>
<td><code>frr_service_down</code></td>
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
</tr>
<tr>
<td><code>host_ip_conflict</code></td>
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
</tr>
<tr>
<td><code>mac_address_conflict</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
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
<td align="center">●</td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center">●</td>
<td align="center">●</td>
<td align="center">○</td>
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
<td align="center"></td>
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
<td align="center">○</td>
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
</tr>
<tr>
<td><code>web_dos_attack</code></td>
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
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">●</td>
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

Candidate generation does not select cases or create release splits. Frozen releases ship under `benchmark/releases/`.
