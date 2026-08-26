# Benchmark configuration reference

This reference is for maintainers and benchmark operators who need to choose a frozen release or a working YAML matrix and understand its artifact layout.

Implementation: [`benchmark/`](../benchmark/) stores matrices and releases; [`workflows/benchmark/`](../src/nika/workflows/benchmark/) loads and executes them.

## Frozen release (`nika-bench@0.1.0`)

Official reproducible suite with **Dev** and **Test** splits (both in-tree):

| Path | Role |
|------|------|
| [`benchmark/releases/0.1.0/`](../benchmark/releases/0.1.0/) | Frozen release: `RELEASE.yaml` + `dev.yaml` + `test.yaml` |
| Identity | `nika-bench@0.1.0` (aliases: `nika@0.1`, `nika@0.1.0`; also `@sha256:<digest>`) |
| Dev | **56** curated incidents, one per included release failure type, for development and debugging |
| Test | **56** held-out instances with the same failures and different scenarios or injection parameters |

```shell
nika benchmark run --release 0.1.0          # release run
nika benchmark run --release 0.1.0 --result_dir results/my-run
nika benchmark releases                     # list + preflight-verify each release
```

Dev/Test case files live under the release directory for maintainers; `--release` uses the suite’s `default_split_for_release` (Test for `0.1.0`) without exposing split knobs. There is no bare default: omit `--release` / `--config` and the CLI errors.

Each release run treats `--result_dir` as **one run** and writes:

| Path | Role |
|------|------|
| `run.json` (and legacy `benchmark_job.json`) | Durable run config: release identity/`benchmark_digest`, `split`, `cases_sha256`, agent/model/`n_trials`, timeout, `official`, stable `run_id` |
| `RELEASE.lock.json` | Slim identity lock |
| `trials/{case_key}__tNN/` | One counted trial (session artifacts) |

Active run progress is recorded under `runtime/benchmark_runs/{run_id}.json`.

After a finished release run, pack and validate a leaderboard submission. See the [leaderboard submission guide](leaderboard-submission.md).

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard pack --result_dir results/my-run --submission results/my-run/submission
nika leaderboard validate results/my-run/YYYYMMDD_slug --source-result-dir results/my-run
```

`defaults.n_trials` in `RELEASE.yaml` (3 for `0.1.0`) expands the split to `case_count × n_trials` deterministic trials. Resume skips completed trials, including `outcome=agent_failed`. NIKA cleans and reruns incomplete trials in place, so retries stay within K. Different `--result_dir` values create isolated runs and do not skip each other's trials.

Per-trial `run.json` is stamped with the same release identity fields plus `trial_id` / `trial_index` / `outcome`.

## Working YAML matrices

| File | Count | Role |
|------|------:|------|
| `benchmark_selected.yaml` | **130** | Editable curated suite with one balanced ISP core per compatible failure and tier, plus healthy baselines (source for freezing a release) |
| `benchmark_full.yaml` | **1,104** | Full scenario × failure × size matrix (74 represented problem IDs) plus healthy baselines |

Ad-hoc `--config` uses the **same** batch orchestrator and `trials/{case_key}__t01/` layout as release runs, with `n_trials=1` (no release `run.json` / `runtime/benchmark_runs` progress unless you go through `--release`).

Each fault case includes an `inject` map that NIKA passes to `nika failure inject` as `--set` flags. Device names must match the target scenario topology. Working matrices carry materialized `root_causes`. NIKA derives these labels from the failure implementation and checks them again during injection. Healthy (no-fault) cases use `problem: healthy` with an empty `inject` map and `root_causes: []`; the runner skips injection and writes `is_anomaly: false` ground truth. Case identity is `scenario` + `problem` + `topo_size` + `inject`, plus the materialized `topo` / `igp` / `bgp_mode` / `rpki` profile for `isp`. ISP uses `topo` only inside benchmark rows; operators select ordinary ISP labs with `-s`. See [Root-cause ground truth and scoring](root-cause-evaluation.md).

```shell
nika benchmark run --config benchmark/benchmark_selected.yaml
nika benchmark run --config benchmark/benchmark_full.yaml
nika benchmark migrate --input path/to/legacy.yaml --output path/to/labeled.yaml --report path/to/report.yaml
```

The migration command accepts a case matrix with a top-level `cases` field, not a release `RELEASE.yaml` manifest. It writes unresolved rows and exits with status 1 unless you pass `--allow-unresolved`.

## Tags

Scenarios and failures declare capability `TAGS`. A failure may run on a scenario only when **every problem tag is present on the scenario** (`problem.TAGS ⊆ scenario.TAGS`). The full matrix is the Cartesian product of tag-compatible pairs (plus topo sizes where required). The selected/release suite picks one traditional Kathara scenario for each included failure; Kubernetes-only failures stay full-matrix-only.

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
| `isp` | `bgp`, `containerlab`, `frr`, `icmp`, `igp`, `isis`, `isp`, `link`, `ospf`, `rpki`, `sndlib`, `srl` |
| `k8s_lab` | `arp`, `bgp`, `coredns`, `fat-tree`, `frr`, `icmp`, `ingress`, `k3s`, `k8s_control_plane`, `k8s_storage`, `k8s_workload`, `kube_proxy`, `kubernetes`, `link`, `mac`, `metallb`, `network_policy`, `pc` |
| `llmd_lab` | `arp`, `coredns`, `http`, `icmp`, `inference`, `k3s`, `k8s_control_plane`, `kube_proxy`, `kubernetes`, `link`, `llm`, `mac`, `metallb`, `network_policy`, `pc` |
| `min3clos` | `bgp`, `clos`, `containerlab`, `fabric`, `link`, `srl` |
| `p4_dc_fabric` | `arp`, `http`, `icmp`, `link`, `mac`, `p4`, `p4_runtime`, `pc` |
| `p4_dc_gateway` | `arp`, `ecn`, `flow_tracking`, `http`, `icmp`, `int`, `link`, `mac`, `p4`, `p4_runtime`, `pc`, `queue`, `telemetry` |
| `sdn_l3_clos` | `arp`, `http`, `icmp`, `link`, `mac`, `pc`, `sdn` |

## Statistics

| Metric | Count |
|--------|------:|
| Registered failure types | 74 |
| Failure types represented in `benchmark_full.yaml` | 74 |
| Full benchmark cases | 1,104 |
| Selected cases | 130 |

### Full matrix by scenario

| Scenario | Cases |
|----------|------:|
| `isp` | 446 |
| `campus_lan` | 114 |
| `p4_dc_gateway` | 108 |
| `dc_clos` | 105 |
| `enterprise_branch` | 105 |
| `p4_dc_fabric` | 84 |
| `sdn_l3_clos` | 78 |
| `k8s_lab` | 27 |
| `llmd_lab` | 24 |
| `min3clos` | 13 |


### Selected / release matrix by scenario

| Scenario | Cases |
|----------|------:|
| `isp` | 55 |
| `campus_lan` | 30 |
| `dc_clos` | 15 |
| `p4_dc_fabric` | 9 |
| `p4_dc_gateway` | 9 |
| `sdn_l3_clos` | 6 |
| `enterprise_branch` | 6 |

Release `0.1.0` still lists a `host_vpn_membership_missing` row on the legacy RIP VPN lab id. Loaders rewrite that id to `wireguard_peer_key_misconfiguration` on `enterprise_branch` with a Site Edge inject target. The same release still lists `link_fragmentation_disabled`; loaders rewrite it to `mtu_mismatch` and update `fault_type` in `root_causes`. The same release still names `p4_counter`; loaders rewrite that id to `p4_dc_fabric` with topo size `s`.

Kubernetes scenarios (`k8s_lab`, `llmd_lab`) and Containerlab `min3clos` appear in the full matrix only; selected/release cases use traditional Kathara labs as the best-matching scenario per failure.

## Coverage matrix (scenario × failure)

Cells mark **capability**, not whether `benchmark_full.yaml` sampled that config. A failure is compatible when its tags and deploy constraints match the scenario and ISP `topo`/`igp`/`bgp_mode` profile. `benchmark_full.yaml` remains a one-config-per-failure runnable sample. Release membership comes from `benchmark/releases/0.1.0/` (dev + test).

Each table has two header rows: scenario, then config (when the scenario has more than one). Cells: blank = incompatible, `○` = compatible, `●` = compatible and in release `0.1.0`. Tables are split by failure subsystem.

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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
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
</tr>
<tr>
<td><code>link_detach</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
</tr>
<tr>
<td><code>link_packet_corruption</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>bgp_asn_misconfig</code></td>
<td align="center"></td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>bgp_blackhole_route_leak</code></td>
<td align="center"></td>
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
<td align="center">○</td>
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
<td><code>bgp_missing_route_advertisement</code></td>
<td align="center"></td>
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
<td align="center">○</td>
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
<td><code>frr_service_down</code></td>
<td align="center">○</td>
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
<td><code>ospf_area_misconfiguration</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
<td><code>ospf_neighbor_missing</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>arp_acl_block</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>bgp_acl_block</code></td>
<td align="center"></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>device_forwarding_packet_corruption</code></td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>dns_port_blocked</code></td>
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
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
<tr>
<td><code>icmp_frag_needed_filter_misconfiguration</code></td>
<td align="center">○</td>
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
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>mtu_mismatch</code></td>
<td align="center">○</td>
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
<td><code>ospf_acl_block</code></td>
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>vrf_dscp_remarking</code></td>
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
</tr>
<tr>
<td><code>wireguard_allowed_ips_misconfiguration</code></td>
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
</tr>
<tr>
<td><code>wireguard_peer_key_misconfiguration</code></td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>load_balancer_overload</code></td>
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
</tr>
<tr>
<td><code>nat_mapping_removed_without_drain</code></td>
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
</tr>
<tr>
<td><code>snat_port_pool_exhaustion</code></td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
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
<td align="center">○</td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>dhcp_missing_subnet</code></td>
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
</tr>
<tr>
<td><code>dhcp_service_down</code></td>
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
</tr>
<tr>
<td><code>dns_lookup_latency</code></td>
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
<td><code>dns_record_error</code></td>
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
<td><code>dns_service_down</code></td>
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
<td><code>host_incorrect_dns</code></td>
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
<td><code>host_incorrect_gateway</code></td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center"></td>
</tr>
<tr>
<td><code>host_incorrect_ip</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>host_incorrect_netmask</code></td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center"></td>
</tr>
<tr>
<td><code>host_ip_conflict</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>host_missing_ip</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>receiver_resource_contention</code></td>
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
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center"></td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>tcp_receive_window_limited</code></td>
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
<th colspan="6">isp</th>
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
<th>abilene-ebgp</th>
<th>abilene-ebgp-rpki</th>
<th>geant-ebgp-rpki</th>
</tr>
</thead>
<tbody>
<tr>
<td><code>arp_cache_poisoning</code></td>
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
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
<td align="center">○</td>
</tr>
<tr>
<td><code>bgp_hijacking</code></td>
<td align="center"></td>
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
<td align="center">○</td>
<td align="center"></td>
<td align="center"></td>
<td align="center"></td>
</tr>
<tr>
<td><code>dhcp_spoofed_dns</code></td>
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
</tr>
<tr>
<td><code>dhcp_spoofed_gateway</code></td>
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
</tr>
<tr>
<td><code>dhcp_spoofed_subnet</code></td>
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
<td align="center">○</td>
<td align="center"></td>
</tr>
<tr>
<td><code>web_dos_attack</code></td>
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
<td align="center">○</td>
<td align="center">○</td>
<td align="center">○</td>
</tr>
</tbody>
</table>

When you add, remove, or retarget cases (new failure, new scenario, or a `TAGS` / `COMPATIBLE_COLUMNS` / registry change that changes compatibility), refresh this section in the same change.

## Regeneration

Regenerate working YAML files:

```shell
uv run python benchmark/generate_benchmark.py
```

Then refresh the capability coverage tables:

```shell
uv run python scripts/render_coverage_matrix.py --write-docs
```

Freeze a new Dev+Test release from the current working YAML (re-selects Test instances from `benchmark_full.yaml`):

```shell
uv run python benchmark/generate_benchmark.py --release 0.2.0
```

Do not pass `--release 0.1.0`. That overwrites the published suite with a newly selected Test split. Bump the version directory for a new official suite.
