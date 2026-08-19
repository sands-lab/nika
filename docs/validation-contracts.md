# Network validation contracts

NIKA scenarios use a validation contract to declare healthy network behavior before deployment. Scenario authors build the contract from topology, addressing, routing, endpoint, service, and policy inputs. Runtime and static verifiers read the same concrete intents and record separate results.

This page is a reference for scenario and verifier maintainers. The v1 implementation covers IPv4 reachability, isolation, path constraints, and BGP or OSPF adjacency.

## Data flow and trust boundary

Scenario construction produces device configuration and the validation contract from the same design inputs. Verifiers must not derive expected behavior from generated FRR configuration, runtime state, forwarding results, or probe output.

```text
design inputs -> configuration generation -> network behavior
             -> validation contract      -> verifier results
```

The contract contains no Batfish query, shell command, CLI command, or probe implementation. Each verifier chooses its execution mechanism and records that mechanism's observations in evidence.

## Contract schema

[`contract.py`](../src/nika/net_env/contract.py) defines the Pydantic schema. Models reject unknown fields and serialize as JSON.

| Model | Purpose |
| --- | --- |
| `ValidationContract` | Schema version, stable contract ID, scenario, design source, and sorted intents |
| `ValidationIntent` | Stable ID, description, property, expected state, level, concrete entities, and property-specific data |
| `NetworkEntity` | Concrete node, endpoint, prefix, or service with an optional address and hosting node |
| `TrafficSelector` | IPv4 protocol; TCP and UDP require a destination port |
| `PathConstraint` | Nodes that traffic must traverse or avoid |
| `AdjacencyExpectation` | BGP or OSPF nodes, peer addresses, protocol fields, and OSPF router IDs |
| `ValidationResult` | Intent ID, verifier, support flag, status, evidence, reason, and duration |
| `ValidationReport` | Contract ID, verifier, aggregate status, coverage, sanity checks, metadata, and one result per intent |

Each property fixes its expected state:

| Property | Expected state | Required fields |
| --- | --- | --- |
| `reachability` | `reachable` | source, destination, traffic |
| `isolation` | `unreachable` | source, destination, traffic |
| `waypoint` | `path_compliant` | source, destination, traffic, path |
| `adjacency` | `established` | adjacency |

Set `level` to `required` when a failed result makes startup validation fail. An `optional` result still records status and evidence but does not change a passing aggregate report.

## Selector expansion

Use `SelectorCatalog` and `EntitySelector` during scenario construction to resolve names and groups. `SelectorCatalog.expand()` sorts and deduplicates entities by name. Store the resulting `NetworkEntity` objects in each intent so verifier behavior does not depend on mutable role or group membership.

The final contract must sort intents by stable ID. Do not include timestamps or runtime-generated identifiers in the contract.

## ISP baseline generation

[`isp/contract.py`](../src/nika/net_env/isp/contract.py) builds the Kathara ISP contract from `IspPlan`, `BgpPlan`, attached edge hosts, and `IspValidationPolicy`.

The current ISP policy generates:

- ICMP reachability between deterministic edge endpoints in one IGP domain.
- Isolation from the first edge endpoint to the design-denied external prefix `192.0.2.0/24`.
- One OSPF adjacency intent per active same-AS link when the selected IGP is OSPF.
- One BGP adjacency intent per planned session and reachability intents for planned BGP observer-prefix pairs.
- An optional avoid constraint when the IGP metric design identifies a node outside every shortest path between the selected edge endpoints.

NIKA does not define a v1 IS-IS adjacency property. The ISP generator omits adjacency intents when IS-IS runs without BGP. The generic ISP scenario has no customer or tenant isolation policy, so `IspValidationPolicy` supplies an explicit denied external prefix instead of treating IGP-reachable infrastructure prefixes as isolated.

The ISP scenario selects a fixed SNDlib topology by name and does not accept topology-size or seed parameters. Contract determinism therefore depends on the topology name, IGP, metric design, BGP mode, traffic attachments, and validation policy.

## Run artifacts

`nika env run` writes the contract and runtime result in the session result directory. An ISP Kathara FRR run with `--static-validation` also writes a Batfish report and snapshot artifacts.

| File | Contents |
| --- | --- |
| `validation-contract.json` | Exact healthy baseline used for the run |
| `validation-results.json` | Verifier report with status and evidence for each intent |
| `validation-batfish.json` | Batfish result, coverage, sanity checks, component versions, and snapshot identity |
| `batfish-snapshot/` | Router inputs, host models, and explicit Layer 1 topology uploaded to Batfish |
| `batfish-snapshot-metadata.json` | Contract ID, topology, snapshot object counts, and Batfish configuration format |

`run.json` records the contract and verifier report filenames. The system log records contract creation and the startup validation payload.

The Kathara ISP runtime verifier uses ICMP or transport probes for reachability and isolation, traceroute for path constraints, and FRR state for adjacency. These commands belong to [`verify.py`](../src/nika/net_env/kathara/isp/isp/verify.py); they must not appear in the contract generator or schema.

## Batfish static verifier

The Phase 1 provider supports ISP, Kathara, FRR, IPv4, OSPF, and BGP. [`snapshot.py`](../src/nika/validation/batfish/snapshot.py) embeds each deployed `/etc/frr/frr.conf` byte sequence in Batfish's [standard snapshot format](https://github.com/batfish/pybatfish/blob/master/docs/source/formats.md). The adapter adds host JSON and `batfish/layer1_topology.json`; it does not generate another routing configuration.

[`compiler.py`](../src/nika/validation/batfish/compiler.py) compiles intents into counterexample searches:

| Contract property | Batfish check | Failure evidence |
| --- | --- | --- |
| Reachability | Search the selected flow space for a failure disposition | Concrete flow and traces |
| Isolation | Search the selected flow space for a successful disposition | Concrete violating flow and path |
| Waypoint | Search for an unreachable flow, forbidden transit, or a path that avoids a required node | Concrete flow and path |
| BGP adjacency | Match compatibility and modeled establishment against nodes, peer addresses, and ASNs | Session status and compatibility fields |
| OSPF adjacency | Match interface addresses, area, nodes, and established compatibility | Session status, including area or timer mismatch |

The verifier also checks configuration parsing, undefined references, duplicate BGP or OSPF router IDs, and forwarding loops. It returns `unsupported` for an intent it cannot compile. Coverage groups results by contract property and separates BGP from OSPF adjacency. Coverage counts unsupported intents without treating them as validation failures.

NIKA records Batfish adjacency results as `static/model-predicted`. Runtime FRR results remain separate evidence for the same intent ID.

### Optional Batfish validation

NIKA runs live runtime verification by default. Set `nika.static_validation.enabled: true` in `config/nika.yaml`, or pass `--static-validation` for one run, to run Batfish before deploying an ISP Kathara FRR scenario:

```shell
uv sync --extra kathara --extra batfish
uv run nika env run isp --topo pdh --igp ospf --static-validation
```

The YAML setting only enables or disables the feature. NIKA keeps Batfish host, port, and verifier selection internal. The optional verifier uses pybatfish `2025.7.7.2423` and starts or reuses the matching pinned Batfish container on its local endpoint. A supported required intent with `failed` or `error` status stops startup before Kathara deployment. An `unsupported` result stays in the report and coverage totals.

The Abilene `ebgp` baseline uses connected AS regions, per-AS IGP domains, and one route reflector per AS. Batfish validates its BGP and OSPF adjacencies, business-prefix reachability, isolation, path constraint, and forwarding-loop sanity check. The pinned Batfish version does not model FRR `match rpki invalid`. When `isp` enables RPKI (`--rpki` / design_source `rpki`), the verifier validates adjacency intents and marks flow or path intents `unsupported`; the runtime verifier checks the deployed FRR behavior.

## Add a verifier

1. Load `validation-contract.json` with `ValidationContract.load()`.
2. Evaluate each intent without changing its expected state or level.
3. Create one `ValidationResult` for each contract intent and use a stable verifier name.
4. Build the aggregate report with `ValidationReport.from_results()` and save it with `ValidationReport.write()`.

`ValidationReport.from_results()` rejects missing, duplicate, and unknown intent IDs. It also rejects a result whose verifier name differs from the report verifier.

## Verify changes

Run the schema, generation, and runtime unit tests before Docker coverage:

```shell
uv run pytest -q tests/nika/net_env/test_validation_contract.py tests/nika/net_env/isp/test_isp_contract.py tests/nika/net_env/isp/test_isp_verify_unit.py
uv run pytest -q tests/nika/validation -k 'not live'
```

Run one small OSPF and one small BGP Docker case after changes to ISP generation or execution:

```shell
uv run pytest -q 'tests/nika/net_env/isp/test_isp_docker.py::IspDockerTest::test_topo_starts_verifies_and_destroys[pdh-ospf]'
uv run pytest -q 'tests/nika/net_env/isp/test_isp_docker.py::IspDockerTest::test_bgp_starts_verifies_and_destroys[pdh-ibgp_rr]'
uv run pytest -q tests/nika/validation/test_batfish_live.py
```
