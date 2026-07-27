# Benchmark configs

## Frozen release (`nika-bench@0.1.0`)

Official reproducible suite with **Dev** and **Test** splits (both in-tree):

| Path | Role |
|------|------|
| [`releases/0.1.0/`](releases/0.1.0/) | Frozen release: `RELEASE.yaml` + `dev.yaml` + `test.yaml` |
| Identity | `nika-bench@0.1.0` (aliases: `nika@0.1`, `nika@0.1.0`; also `@sha256:<digest>`) |
| Dev | **56** curated incidents (one per failure type) — development / debugging |
| Test | **56** held-out instances (same failures, different scenario/inject) — held-out eval |

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

Active orchestrator progress (trial counts / status / pointer to `result_dir`) is recorded under `runtime/benchmark_runs/{run_id}.json`, not via environment variables.

```shell
nika benchmark run --release 0.1.0 --result_dir results/my-run
```

After a finished release run, pack and validate a leaderboard submission (see [`docs/leaderboard-submission.md`](../docs/leaderboard-submission.md)):

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard pack --result_dir results/my-run --submission results/my-run/submission
nika leaderboard validate results/my-run/YYYYMMDD_slug --source-result-dir results/my-run
```

`defaults.n_trials` in `RELEASE.yaml` (3 for `0.1.0`) expands the split to `case_count × n_trials` deterministic trials. Resume skips completed trials (including `outcome=agent_failed`); incomplete trials are cleaned and re-run in place so retries never inflate past K. Different `--result_dir` values are isolated runs and never skip each other.

Per-trial `run.json` is stamped with the same release identity fields plus `trial_id` / `trial_index` / `outcome`.


Regenerate working YAML (not frozen releases):

```shell
uv run python benchmark/generate_benchmark.py
```

Regenerate / refresh the frozen Dev+Test release from selected + full matrices:

```shell
uv run python benchmark/generate_heldout.py
```

Ship a new version directory (e.g. `0.1.1`) instead of silently mutating an already published suite when results are in flight.

## Working YAML (development)

| File | Count | Role |
|------|------:|------|
| `benchmark_selected.yaml` | **56** | Editable curated suite (source for freezing a release) |
| `benchmark_full.yaml` | **702** | Full scenario × failure × size matrix |

Ad-hoc `--config` uses the **same** batch orchestrator and `trials/{case_key}__t01/` layout as release runs, with `n_trials=1` (no release `run.json` / `runtime/benchmark_runs` progress unless you go through `--release`).

Each case includes an `inject` map (device names, etc.) that is passed to `nika failure inject` as `--set` flags. Device names must match the target scenario topology (see lab definitions under `src/nika/net_env/`). IP and netmask values are derived from the live lab at inject time.

```shell
nika benchmark run --config benchmark/benchmark_selected.yaml
nika benchmark run --config benchmark/benchmark_full.yaml
```

## Tags

Scenarios and failures declare capability `TAGS`. A failure may run on a scenario only when **every problem tag is present on the scenario** (`problem.TAGS ⊆ scenario.TAGS`). The full matrix is the Cartesian product of tag-compatible pairs (plus topo sizes where required). The selected/release suite picks one traditional Kathara scenario per failure.

### Tag meanings

| Tag | Meaning |
|-----|---------|
| `arp` | ARP / L2 neighbor resolution present |
| `bgp` | BGP routing (FRR or equivalent) |
| `bloom_filter` | P4 bloom-filter program |
| `clos` | Clos / leaf-spine style fabric |
| `containerlab` | Containerlab backend (not Kathara) |
| `dhcp` | DHCP server / clients in the lab |
| `dns` | DNS server / resolver path |
| `fabric` | Multi-switch fabric topology |
| `fat-tree` | Fat-tree underlay (k8s lab) |
| `frr` | FRRouting daemons on routers |
| `http` | HTTP / web service endpoints |
| `icmp` | ICMP reachability usable for diagnosis |
| `inference` | LLM inference workload (llmd) |
| `int` | P4 In-band Network Telemetry |
| `k3s` | Lightweight Kubernetes (k3s) |
| `kubernetes` | Kubernetes control/data plane |
| `link` | Controllable L2/L3 links (down, flap, QoS, …) |
| `llm` | LLM-serving scenario features |
| `load_balancer` | Load-balancer node/service |
| `mac` | MAC addressing / L2 identity |
| `mpls` | P4 MPLS label stack |
| `ospf` | OSPF intradomain routing |
| `p4` | BMv2 / P4 switches |
| `pc` | End hosts (PCs) that can be misconfigured |
| `sdn` | SDN controller + OpenFlow switches |
| `srl` | Nokia SR Linux NOS (Containerlab) |
| `vpn` | VPN membership / tunnels |
| `web` | Web-tier services (alias capability for HTTP labs) |

### Scenario tags

| Scenario | Tags |
|----------|------|
| `dc_clos_bgp` | `arp`, `bgp`, `frr`, `icmp`, `link`, `mac`, `pc` |
| `dc_clos_service` | `arp`, `bgp`, `dns`, `frr`, `http`, `icmp`, `link`, `mac`, `pc` |
| `k8s_lab` | `arp`, `bgp`, `fat-tree`, `frr`, `icmp`, `k3s`, `kubernetes`, `link`, `mac`, `pc` |
| `llmd_lab` | `arp`, `http`, `icmp`, `inference`, `k3s`, `kubernetes`, `link`, `llm`, `mac`, `pc` |
| `min3clos` | `bgp`, `clos`, `containerlab`, `fabric`, `link`, `srl` |
| `ospf_enterprise_dhcp` | `arp`, `dhcp`, `dns`, `frr`, `http`, `icmp`, `link`, `load_balancer`, `mac`, `ospf`, `pc`, `web` |
| `ospf_enterprise_static` | `arp`, `frr`, `http`, `icmp`, `link`, `mac`, `ospf`, `pc` |
| `p4_bloom_filter` | `arp`, `bloom_filter`, `icmp`, `link`, `mac`, `p4`, `pc` |
| `p4_counter` | `arp`, `icmp`, `link`, `mac`, `p4`, `pc` |
| `p4_int` | `arp`, `icmp`, `int`, `link`, `mac`, `p4`, `pc` |
| `p4_mpls` | `arp`, `icmp`, `link`, `mac`, `mpls`, `p4`, `pc` |
| `rip_small_internet_vpn` | `arp`, `frr`, `http`, `icmp`, `link`, `mac`, `pc`, `vpn` |
| `sdn_clos` | `arp`, `icmp`, `link`, `mac`, `pc`, `sdn` |
| `sdn_star` | `arp`, `icmp`, `link`, `mac`, `pc`, `sdn` |
| `simple_bgp` | `arp`, `bgp`, `frr`, `icmp`, `link`, `mac`, `pc` |

## Statistics

| Metric | Count |
|--------|------:|
| Failure types (root causes) | 56 |
| Full benchmark cases | 702 |
| Selected / release 0.1.0 cases | 56 |
| Scenarios in full matrix | 15 |

### Full matrix by scenario

| Scenario | Cases |
|----------|------:|
| `ospf_enterprise_dhcp` | 111 |
| `dc_clos_service` | 102 |
| `ospf_enterprise_static` | 78 |
| `rip_small_internet_vpn` | 72 |
| `dc_clos_bgp` | 69 |
| `sdn_clos` | 57 |
| `sdn_star` | 57 |
| `k8s_lab` | 23 |
| `simple_bgp` | 23 |
| `llmd_lab` | 20 |
| `p4_bloom_filter` | 20 |
| `p4_mpls` | 20 |
| `p4_counter` | 19 |
| `p4_int` | 19 |
| `min3clos` | 12 |

### Selected / release matrix by scenario

| Scenario | Cases |
|----------|------:|
| `ospf_enterprise_dhcp` | 26 |
| `dc_clos_bgp` | 13 |
| `p4_bloom_filter` | 6 |
| `sdn_clos` | 5 |
| `ospf_enterprise_static` | 3 |
| `dc_clos_service` | 1 |
| `p4_mpls` | 1 |
| `rip_small_internet_vpn` | 1 |

Kubernetes scenarios (`k8s_lab`, `llmd_lab`) and Containerlab `min3clos` appear in the full matrix only; selected/release cases use traditional Kathara labs as the best-matching scenario per failure.

## Coverage matrix (scenario × failure)

Compatibility from `benchmark_full.yaml` (tag match). Cells ignore topo size: a `✓` means the pair appears at least once in the full matrix.

| Symbol | Meaning |
|--------|---------|
| ★ | Included in selected / release `0.1.0` |
| ✓ | Present in full matrix only |
| (blank) | Not tag-compatible |

Column abbreviations:

| Abbr | Scenario |
|------|----------|
| `dc_bgp` | `dc_clos_bgp` |
| `dc_svc` | `dc_clos_service` |
| `k8s` | `k8s_lab` |
| `llmd` | `llmd_lab` |
| `min3` | `min3clos` |
| `ospf_d` | `ospf_enterprise_dhcp` |
| `ospf_s` | `ospf_enterprise_static` |
| `p4_bf` | `p4_bloom_filter` |
| `p4_ct` | `p4_counter` |
| `p4_int` | `p4_int` |
| `p4_mp` | `p4_mpls` |
| `rip_vpn` | `rip_small_internet_vpn` |
| `sdn_c` | `sdn_clos` |
| `sdn_s` | `sdn_star` |
| `s_bgp` | `simple_bgp` |

| Failure | `dc_bgp` | `dc_svc` | `k8s` | `llmd` | `min3` | `ospf_d` | `ospf_s` | `p4_bf` | `p4_ct` | `p4_int` | `p4_mp` | `rip_vpn` | `sdn_c` | `sdn_s` | `s_bgp` |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `arp_acl_block` | ✓ | ✓ | ✓ | ✓ |  | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `arp_cache_poisoning` | ✓ | ✓ | ✓ | ✓ |  | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `bgp_acl_block` | ★ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |
| `bgp_asn_misconfig` | ★ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |
| `bgp_blackhole_route_leak` | ★ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |
| `bgp_hijacking` | ✓ | ★ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |
| `bgp_missing_route_advertisement` | ★ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |
| `bmv2_switch_down` |  |  |  |  |  |  |  | ★ | ✓ | ✓ | ✓ |  |  |  |  |
| `dhcp_missing_subnet` |  |  |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dhcp_service_down` |  |  |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dhcp_spoofed_dns` |  |  |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dhcp_spoofed_gateway` |  |  |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dhcp_spoofed_subnet` |  |  |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dns_lookup_latency` |  | ✓ |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dns_port_blocked` |  | ✓ |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dns_record_error` |  | ✓ |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `dns_service_down` |  | ✓ |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `flow_rule_loop` |  |  |  |  |  |  |  |  |  |  |  |  | ★ | ✓ |  |
| `flow_rule_shadowing` |  |  |  |  |  |  |  |  |  |  |  |  | ★ | ✓ |  |
| `frr_service_down` | ✓ | ✓ | ✓ |  |  | ★ | ✓ |  |  |  |  | ✓ |  |  | ✓ |
| `host_crash` | ★ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `host_incorrect_dns` |  | ✓ |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `host_incorrect_gateway` | ✓ | ✓ | ✓ |  |  | ★ | ✓ |  |  |  |  | ✓ |  |  | ✓ |
| `host_incorrect_ip` | ✓ | ✓ | ✓ | ✓ |  | ✓ | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `host_incorrect_netmask` | ✓ | ✓ | ✓ |  |  | ✓ | ★ |  |  |  |  | ✓ |  |  | ✓ |
| `host_ip_conflict` | ★ | ✓ | ✓ | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `host_missing_ip` | ✓ | ✓ | ✓ | ✓ |  | ✓ | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `host_static_blackhole` | ★ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |  | ✓ |
| `host_vpn_membership_missing` |  |  |  |  |  |  |  |  |  |  |  | ★ |  |  |  |
| `http_acl_block` |  | ✓ |  | ✓ |  | ★ | ✓ |  |  |  |  | ✓ |  |  |  |
| `icmp_acl_block` | ✓ | ✓ | ✓ | ✓ |  | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `incast_traffic_network_limitation` |  | ✓ |  | ✓ |  | ★ | ✓ |  |  |  |  | ✓ |  |  |  |
| `link_bandwidth_throttling` | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `link_detach` | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `link_down` | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `link_flap` | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `link_fragmentation_disabled` | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `link_high_packet_corruption` | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `load_balancer_overload` |  |  |  |  |  | ★ |  |  |  |  |  |  |  |  |  |
| `mac_address_conflict` | ✓ | ✓ | ✓ | ✓ |  | ★ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `mpls_label_limit_exceeded` |  |  |  |  |  |  |  |  |  |  | ★ |  |  |  |  |
| `ospf_acl_block` |  |  |  |  |  | ★ | ✓ |  |  |  |  |  |  |  |  |
| `ospf_area_misconfiguration` |  |  |  |  |  | ★ | ✓ |  |  |  |  |  |  |  |  |
| `ospf_neighbor_missing` |  |  |  |  |  | ★ | ✓ |  |  |  |  |  |  |  |  |
| `p4_aggressive_detection_thresholds` |  |  |  |  |  |  |  | ★ |  |  |  |  |  |  |  |
| `p4_compilation_error_parser_state` |  |  |  |  |  |  |  | ★ | ✓ | ✓ | ✓ |  |  |  |  |
| `p4_header_definition_error` |  |  |  |  |  |  |  | ★ | ✓ | ✓ | ✓ |  |  |  |  |
| `p4_table_entry_misconfig` |  |  |  |  |  |  |  | ★ | ✓ | ✓ | ✓ |  |  |  |  |
| `p4_table_entry_missing` |  |  |  |  |  |  |  | ★ | ✓ | ✓ | ✓ |  |  |  |  |
| `receiver_resource_contention` |  | ✓ |  | ✓ |  | ★ | ✓ |  |  |  |  | ✓ |  |  |  |
| `sdn_controller_crash` |  |  |  |  |  |  |  |  |  |  |  |  | ★ | ✓ |  |
| `sender_application_delay` |  | ✓ |  | ✓ |  | ★ | ✓ |  |  |  |  | ✓ |  |  |  |
| `sender_resource_contention` |  | ✓ |  | ✓ |  | ★ | ✓ |  |  |  |  | ✓ |  |  |  |
| `southbound_port_block` |  |  |  |  |  |  |  |  |  |  |  |  | ★ | ✓ |  |
| `southbound_port_mismatch` |  |  |  |  |  |  |  |  |  |  |  |  | ★ | ✓ |  |
| `web_dos_attack` |  | ✓ |  | ✓ |  | ★ | ✓ |  |  |  |  | ✓ |  |  |  |

## Regeneration

Regenerate working YAML files (not frozen releases):

```shell
uv run python benchmark/generate_benchmark.py
```

Refresh the frozen Dev+Test release (updates `dev.yaml`, `test.yaml`, `RELEASE.yaml`):

```shell
uv run python benchmark/generate_heldout.py
```

Prefer bumping the version directory when publishing a new official suite.
