# nika-bench 0.1.0 (deprecated)

Provenance-only snapshot. `nika benchmark releases` marks it `DEPRECATED`. You cannot load it with `nika benchmark run --release`.

Use [0.2.0](../0.2.0/README.md) for current runs. Machine metadata remains in [`RELEASE.yaml`](RELEASE.yaml); cases are in [`dev.yaml`](dev.yaml) and [`test.yaml`](test.yaml).

## Suite shape

| Split | Cases file | Cases |
| --- | --- | ---: |
| `dev` | `dev.yaml` | 56 |
| `test` | `test.yaml` | 56 |

Across both splits: **112** rows, **10** legacy scenario IDs, **56** failure IDs (no healthy baselines). Scenario and failure IDs are legacy strings; loaders do not rewrite them to current registry names.

## Scenario coverage (legacy IDs)

Counts sum `dev` + `test`. These IDs are not in the current scenario catalog. For today's scenarios, read [Network scenario reference](../../../docs/operations/network-scenarios.md).

| Legacy scenario | Cases | Distinct problems |
| --- | ---: | ---: |
| `ospf_enterprise_dhcp` | 32 | 26 |
| `dc_clos_service` | 25 | 25 |
| `dc_clos_bgp` | 23 | 23 |
| `p4_bloom_filter` | 7 | 6 |
| `ospf_enterprise_static` | 6 | 6 |
| `p4_counter` | 5 | 5 |
| `sdn_clos` | 5 | 5 |
| `sdn_star` | 5 | 5 |
| `p4_mpls` | 2 | 1 |
| `rip_small_internet_vpn` | 2 | 1 |

Rough mapping to current docs (names differ; do not treat as identity):

- Clos / BGP / service → [Data-center Clos](../../../docs/operations/network-scenarios.md#data-center-clos-scenario)
- OSPF enterprise → [Campus LAN](../../../docs/operations/network-scenarios.md#campus-lan-scenario) / [Enterprise Branch](../../../docs/operations/network-scenarios.md#enterprise-branch-vpn-scenario)
- SDN → [SDN scenarios](../../../docs/operations/network-scenarios.md#sdn-scenarios)
- P4 → [P4 scenarios](../../../docs/operations/network-scenarios.md#p4-scenarios)

## Failure coverage

Legacy failure IDs overlap partially with today's registry. Authoritative current taxonomy: [Failure taxonomy and reference](../../../docs/operations/failures.md). Current release membership matrix: [Coverage matrix (0.2.0)](../../../docs/benchmarks/benchmark-configuration.md#coverage-matrix-scenario--failure).

## Scoring (when this version was active)

Rule-based RCA F1 (`n_trials=3`). See [Root-cause evaluation](../../../docs/benchmarks/root-cause-evaluation.md) for the current scoring contract.
