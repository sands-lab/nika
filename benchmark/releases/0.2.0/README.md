# nika-bench 0.2.0

Frozen Dev/Test suite for evaluating network troubleshooting agents on injectable faults. Use this release for official runs and leaderboard submissions. Working matrices under `benchmark/working/` are for development only.

Machine loaders read [`RELEASE.yaml`](RELEASE.yaml). Case rows are in [`dev.yaml`](dev.yaml) and [`test.yaml`](test.yaml).

## Run

```shell
nika benchmark releases
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
```

Default split is `test`. Each case runs `n_trials=3` times (`case_count × 3` deterministic trials). Official and leaderboard scoring use rule-based RCA F1 only (`leaderboard_primary: rca_f1`; `judge_allowed: false`). Optional `nika eval judge` output is for local analysis and does not count toward submissions.

Operator reference: [Benchmark configuration](../../../docs/benchmarks/benchmark-configuration.md). Leaderboard pack/validate: [Leaderboard submission](../../../docs/benchmarks/leaderboard-submission.md).

## Suite shape

| Split | Cases file | Cases |
| --- | --- | ---: |
| `dev` | `dev.yaml` | 84 |
| `test` | `test.yaml` | 85 |

Across both splits: **169** executable rows, **29** scenario IDs, **75** registered failures plus **healthy** baselines (18 healthy rows). Docs state that both splits cover every registered failure; labels are public.

## Scenario coverage

Counts below sum `dev` + `test`. Scenario behavior and deploy options: [Network scenario reference](../../../docs/operations/network-scenarios.md).

| Scenario | Cases | Distinct problems | Docs |
| --- | ---: | ---: | --- |
| `p4_dc_gateway` | 30 | 23 | [P4 scenarios](../../../docs/operations/network-scenarios.md#p4-scenarios) |
| `campus_lan` | 21 | 15 | [Campus LAN](../../../docs/operations/network-scenarios.md#campus-lan-scenario) |
| `enterprise_branch` | 21 | 15 | [Enterprise Branch VPN](../../../docs/operations/network-scenarios.md#enterprise-branch-vpn-scenario) |
| `sdn_l3_clos` | 17 | 11 | [SDN scenarios](../../../docs/operations/network-scenarios.md#sdn-scenarios) |
| `dc_clos` | 13 | 11 | [Data-center Clos](../../../docs/operations/network-scenarios.md#data-center-clos-scenario) |
| `llmd_lab` | 11 | 11 | [Kubernetes scenarios](../../../docs/operations/network-scenarios.md#kubernetes-scenarios) |
| `p4_dc_fabric` | 11 | 11 | [P4 scenarios](../../../docs/operations/network-scenarios.md#p4-scenarios) |
| `k8s_lab` | 10 | 10 | [Kubernetes scenarios](../../../docs/operations/network-scenarios.md#kubernetes-scenarios) |
| `min3clos` | 3 | 3 | [Containerlab Clos](../../../docs/operations/network-scenarios.md#containerlab-clos-scenario) |
| 20× `isp_*` topologies | 32 | (per-topology) | [SNDlib ISP scenarios](../../../docs/operations/network-scenarios.md#sndlib-isp-scenarios) |

ISP IDs in this release: `isp_abilene`, `isp_abilene_ebgp_rpki`, `isp_abilene_ebgp_rtbh`, `isp_cost266`, `isp_dfn-bwin`, `isp_dfn-bwin_ebgp_rtbh`, `isp_dfn-gwin`, `isp_di-yuan`, `isp_geant`, `isp_geant_ebgp_rpki`, `isp_germany50`, `isp_india35`, `isp_janos-us`, `isp_janos-us-ca`, `isp_nobel-eu`, `isp_nobel-germany`, `isp_pdh`, `isp_pioro40`, `isp_ta1`, `isp_ta2`.

List installed scenarios with `uv run nika env list`.

## Failure coverage

Failures are grouped by `failure_domain`. Taxonomy, injection method, and per-failure parameters: [Failure taxonomy and reference](../../../docs/operations/failures.md). Capability and release membership cells: [Coverage matrix](../../../docs/benchmarks/benchmark-configuration.md#coverage-matrix-scenario--failure).

| Domain | Distinct failures in release | Docs |
| --- | ---: | --- |
| Forwarding, Encapsulation & Policy | 26 | [section](../../../docs/operations/failures.md#forwarding-encapsulation--policy) |
| Addressing, Neighbor & Naming | 13 | [section](../../../docs/operations/failures.md#addressing-neighbor--naming) |
| Routing & Control Plane | 8 | [section](../../../docs/operations/failures.md#routing--control-plane) |
| Security | 7 | [section](../../../docs/operations/failures.md#security) |
| Link & Interface | 6 | [section](../../../docs/operations/failures.md#link--interface) |
| Service Networking | 6 | [section](../../../docs/operations/failures.md#service-networking) |
| Management & Orchestration Plane | 4 | [section](../../../docs/operations/failures.md#management--orchestration-plane) |
| Traffic, Queueing & Resource | 3 | [section](../../../docs/operations/failures.md#traffic-queueing--resource) |
| Endpoint & Application | 2 | [section](../../../docs/operations/failures.md#endpoint--application) |

Inspect a failure ID with `uv run nika failure describe <failure_id>`. Root-cause scoring: [Root-cause evaluation](../../../docs/benchmarks/root-cause-evaluation.md).

## Runtime requirements

Allowed MCP servers and required container images are declared in [`RELEASE.yaml`](RELEASE.yaml) (`tools.allowed_mcp_servers`, `images.required`). Preflight checks them when you run a release. MCP overview: [MCP servers](../../../docs/agents/mcp-servers.md).
