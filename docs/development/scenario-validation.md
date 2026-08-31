# Scenario validation

This reference is for scenario and verifier maintainers who define a healthy baseline for a registered network scenario. Read [Network scenarios](../operations/network-scenarios.md) to select and configure a scenario.

Every current scenario uses a **two-layer** healthy baseline, aligned with failure inject (`verify_fault` vs test-path `evaluate_symptom`):

| Layer | When | Scenario API | Purpose |
| --- | --- | --- | --- |
| Fast runtime | `nika env run` startup | `startup_verify_lab()` (+ Batfish for ISP Kathara+FRR) | Bounded readiness: nodes, control-plane convergence, minimal connectivity |
| Full behavioral | Tests only | `verify_lab()` via `tests.support.scenario_evaluate.evaluate_scenario` | All documented healthy intents: services, isolation, ECMP, HTTP, contract intents |

`verify_lab_with_retry()` polls `startup_verify_lab()` when implemented; otherwise it falls back to `verify_lab()`. Production workflows do not import `evaluate_scenario`.

Base `isp_<topology>` scenarios on Kathara with FRR also create a design-time validation contract and can run Batfish static validation. Static and runtime evidence remain separate because they answer different questions.

## Validation flow and artifacts

Scenario construction derives topology, addressing, routing, endpoints, and policy from design inputs. A scenario that implements `get_validation_contract()` creates concrete expected intents from those inputs. Do not derive expected behavior from generated configuration, probe output, or runtime forwarding state.

```text
design inputs -> configuration generation -> deployed network -> runtime verification
             -> validation contract      -> Batfish verification (when supported)
```

`nika env run` deploys the lab, polls `startup_verify_lab()` (or `verify_lab()` when no startup hook exists) until it passes or the configured timeout expires, and records its checks in the session log. A contract-bearing scenario writes the contract before deployment. With `--static-validation`, a supported run performs Batfish validation before deployment and fails startup when a supported required intent fails or errors.

| Artifact | When NIKA writes it | Contents |
| --- | --- | --- |
| `validation-contract.json` | A scenario returns a contract | Healthy design baseline and sorted concrete intents |
| `validation-results.json` | Runtime verifier returns a contract report | Per-intent runtime evidence |
| `batfish-validation.json` | Supported static validation runs | Batfish result, coverage, sanity checks, versions, and snapshot identity |
| `batfish-snapshot/` | Supported static validation runs | Router inputs, host models, and Layer 1 topology sent to Batfish |
| `batfish-snapshot-metadata.json` | Supported static validation runs | Contract ID, topology, object counts, and snapshot format |
| `batfish-validation-faulty.json` | Failure-effect validation rebuilds a supported faulty snapshot | Static result after injection |
| `validation-results-faulty.json` | Failure-effect validation runs against a contract-aware live lab | Runtime result after injection |

For a scenario without a contract, `verify_lab()` returns `verified`, boolean `checks`, and optional diagnostic `details`. It remains a startup gate, but it does not create a `ValidationReport` artifact.

## Scenario coverage

The scenario registry in [`net_env_pool.py`](../../src/nika/net_env/net_env_pool.py) is the source of truth.

| Scenario | Backend | Healthy intent | Batfish static validation | Runtime verifier |
| --- | --- | --- | --- | --- |
| `dc_clos` | Kathara | eBGP Clos fabric with DNS and HTTP services |  | [`dc_clos/verify.py`](../../src/nika/net_env/dc_clos/verify.py) |
| `campus_lan` | Kathara | Multi-area OSPF campus with DHCP services |  | [`campus_lan/verify.py`](../../src/nika/net_env/campus_lan/verify.py) |
| `enterprise_branch` | Kathara | WireGuard and BGP overlay with VRF isolation |  | [`enterprise_branch/verify.py`](../../src/nika/net_env/enterprise_branch/verify.py) |
| `sdn_l3_clos` | Kathara | ONOS-controlled OVS L3 Clos and ECMP |  | [`sdn_l3_clos/verify.py`](../../src/nika/net_env/sdn_l3_clos/verify.py) |
| `p4_dc_fabric` | Kathara | P4Runtime-programmed Clos forwarding and ECMP |  | [`p4_dc_fabric/verify.py`](../../src/nika/net_env/p4_dc_fabric/verify.py) |
| `p4_dc_gateway` | Kathara | P4 gateway-spine-leaf service path and complete INT-MX telemetry |  | [`p4_dc_gateway/verify.py`](../../src/nika/net_env/p4_dc_gateway/verify.py) |
| `isp_<topology>` | Kathara or Containerlab | SNDlib IGP/BGP forwarding, adjacency, and policy | Kathara + FRR only | [`kathara/verify.py`](../../src/nika/net_env/isp/kathara/verify.py), [`containerlab/verify.py`](../../src/nika/net_env/isp/containerlab/verify.py) |
| `min3clos` | Containerlab | SR Linux eBGP Clos client connectivity |  | [`min3clos/verify.py`](../../src/nika/net_env/min3clos/verify.py) |
| `k8s_lab` | Kathara | BGP fat-tree and k3s Word application |  | [`k8s_lab/verify.py`](../../src/nika/net_env/k8s_lab/verify.py) |
| `llmd_lab` | Kathara | L2 k3s inference cluster and gateway API |  | [`llmd_lab/verify.py`](../../src/nika/net_env/llmd_lab/verify.py) |

## Runtime validation by scenario

Each row below corresponds to one runtime check or one generated family of checks. A row passes when its expected result holds. NIKA accepts the scenario only when all runtime checks pass.

### `dc_clos`

`dc_clos` provides an eBGP Clos fabric that connects DNS and HTTP endpoints on different leaves with external clients.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| `host`: sampled fabric nodes and `pc_0_0`/`pc_0_1` | Compare expected names with `runtime.list_nodes()` | Confirm minimum fabric deployment | Every expected node exists |
| `super_spine_router_0` | Parse `show bgp summary` | Confirm fabric BGP convergence | At least two neighbors are established |
| `leaf_router_0_0` | Parse `show bgp summary` | Confirm leaf BGP convergence | At least one neighbor is established |
| `pc_0_0`, `pc_0_1` | Inspect `eth0` IPv4 address | Confirm endpoint addressing | Addresses `10.0.0.2` and `10.0.1.2` are present |
| `pc_0_0` to gateway and remote leaf host | ICMP ping to `10.0.0.1` and `10.0.1.2` | Confirm first-hop and cross-leaf forwarding | One reply from each target |
| `service`: sampled fabric, DNS, Web, and client nodes | Compare expected names with `runtime.list_nodes()` | Confirm service topology deployment | Every expected node exists |
| `super_spine_router_0`, `client_0` | BGP summary and client IPv4 inspection | Confirm routing convergence and client addressing | BGP establishes; client has `192.168.0.2` |
| `client_0` to DNS and Web endpoints | ICMP ping to `10.0.0.2` and `10.0.1.2` | Confirm client-to-pod forwarding | One reply from each target |
| `dns_pod0`, `client_0` | Check `named`; request `http://web0.pod0/` | Confirm DNS service and Web delivery | `named` is active; HTTP returns `200` |

### `campus_lan`

`campus_lan` provides a hierarchical multi-area OSPF campus with cross-branch connectivity, DHCP relay, DNS, HTTP, and a load-balanced service.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| `router_core_1` | Query OSPF process and neighbors; read `eth0` state | Confirm core routing convergence | Routing process exists, at least two `Full` neighbors, and link is `up` |
| Workload-specific distribution router | Inspect `br0` IPv4 address | Confirm access gateway configuration | `10.1.1.1` is present |
| `pc_1_1_1_1` | Inspect IPv4 address and default route | Confirm client attachment | IPv4 exists and default route uses `10.1.1.1` |
| Probe host to gateway, peer, and DNS server | ICMP ping | Confirm access, cross-branch, and farm routing | One reply from every target |
| `dns_server` and probe host | Check `named`; resolve `web0.local` | Confirm DNS service and record | `named` is active; answer includes `10.200.0.3` |
| Probe and peer hosts | Request `web0.local` and `web3.local` | Confirm Web access from both branches | Both requests return `200` |
| DHCP attachment | Inspect lease; check DHCP server and relay | Confirm dynamic attachment | Address is in the configured range; DHCP server and relay run |
| Load-balanced service | Check Web service and NGINX; resolve and request `web99.local` | Confirm service path | Services run, DNS resolves, and HTTP returns `200` |

### `enterprise_branch`

`enterprise_branch` connects HQ, DC2, and branches through a dual-provider WireGuard and eBGP overlay while keeping CORP and shared SERVER traffic separate from GUEST and IOT VRFs. Branch-to-branch business traffic uses the overlay. Providers must not learn enterprise prefixes. Overlay interfaces carry the configured EF and BE QoS classes.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| All nodes and business hosts from `TopoSpec` | Compare deployed names; inspect each host IPv4 address | Confirm topology and LAN addressing | Every node exists; each host has its designed address |
| Every site edge | List VRF devices | Confirm tenant routing domains | Each designed CORP, SERVER, GUEST, or IOT VRF exists |
| Each designed WireGuard tunnel | Ping underlay in both directions; inspect `wg show` | Confirm tunnel endpoint reachability and interface state | Both pings reply; both WireGuard states are non-empty |
| Each tunnel endpoint | Parse `show bgp summary` for peer tunnel address | Confirm overlay route exchange | Every designed BGP peer establishes |
| Every branch edge | Inspect overlay BGP and CORP VRF RIB | Confirm CORP/SERVER distribution | HQ CORP and SERVER prefixes are present |
| Every branch and local-only VRF | Inspect BGP and VRF RIBs | Enforce GUEST/IOT isolation | Local-only prefixes are absent from CORP; remote CORP/SERVER is absent from local-only VRFs |
| Branch, HQ, and DC2 CORP hosts | ICMP and HTTP probes | Confirm business connectivity and shared service access | Required pings reply; HQ SERVER HTTP returns `200` |
| Branch-pair CORP hosts | Ping and inspect `ip route get` | Confirm overlay forwarding | Ping replies; route uses `wg*` or a `172.30.*` next hop |
| Provider routers | Inspect kernel and FRR routes | Prevent enterprise-prefix leakage | No advertised enterprise prefix appears |
| GUEST/IOT hosts | Ping remote and same-site CORP hosts | Enforce data-plane isolation | Both probes receive no reply |
| Every WireGuard interface | Inspect `tc qdisc` and `tc class` | Confirm overlay QoS | HTB qdisc, classes `1:10` and `1:20`, and designed rate exist |
| Backup and dual-homed spoke sessions | Parse BGP summary and best path | Confirm resilience and primary preference | Backup peers establish; HQ CORP best path uses primary peer |

### `sdn_l3_clos`

`sdn_l3_clos` provides L3 forwarding through an ONOS-controlled OVS Clos fabric, with SELECT groups for ECMP rather than bridge learning or STP.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| ONOS, fabric manager, modeled switches, and endpoints | Compare expected names with deployed nodes | Confirm control and data-plane deployment | Every expected node exists |
| `onos` | Check `eth0` state and Java/ONOS process | Confirm controller readiness | Link is `up`; Java or ONOS process exists |
| Sampled leaves and spines | Run `ovs-vsctl show` | Confirm OVS initialization | Each command returns OVS state |
| ONOS topology and modeled OpenFlow devices | Query ONOS topology snapshot | Confirm controller sessions and discovered fabric | All expected IDs are available; sufficient links exist |
| Sampled OVS switches | Inspect flows, bridge state, and groups | Enforce controller-programmed ECMP | Virtual MAC matches; no `NORMAL` or enabled RSTP; sufficient SELECT buckets |
| Sampled leaves and spines | Compare expected rules with OVS flow/group state | Confirm controller and dataplane agree | IPv4 flows exist; leaves have required groups |
| Representative client and remote Web endpoint | ICMP ping and HTTP request | Confirm cross-rack service path | Ping replies; HTTP returns `200` |
| Sampled endpoints | Inspect IPv4 and default route | Confirm endpoint L3 setup | Designed IPv4 and gateway are present |

### `p4_dc_fabric`

`p4_dc_fabric` programs BMv2 switches through P4Runtime with the generated forwarding intent, gateway ARP entries, cross-rack L3 forwarding, and multi-path ECMP.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| Fabric manager, modeled spines/leaves, and endpoints | Compare expected names with deployed nodes | Confirm fabric deployment | Every expected node exists |
| Sampled leaves and spine | Query BMv2 gRPC | Confirm P4Runtime server readiness | Every sampled switch responds |
| Modeled devices and interfaces | Probe out-of-band addresses; inspect interface state | Confirm management and physical links | Probes succeed; expected interfaces are `up` |
| Fabric manager | Load saved forwarding intent | Confirm control input exists | Intent contains switch entries |
| P4Runtime read-back | Compare tables and pipeline with generated intent | Confirm switch programming | No intent-versus-observed mismatch |
| Same-rack, cross-rack, and multi-rack endpoint pairs | ICMP ping | Confirm L3 forwarding across each scope | Every probe replies |
| Representative cross-rack client and Web endpoint | HTTP request | Confirm application forwarding | HTTP returns `200` |
| Sampled cross-rack flow and spine counters | Send probes and compare ingress counters | Confirm ECMP multipath use | At least two spine counters increase |
| Sampled endpoints | Inspect IPv4 and gateway neighbor | Confirm host setup and first hop | Designed IPv4 exists; neighbor has virtual-router MAC |

### `p4_dc_gateway`

`p4_dc_gateway` provides a programmable gateway-spine-leaf service fabric with a live P4Runtime control plane and telemetry collector.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| Modeled gateways, spines, leaves, endpoints, `fabric_mgr`, and `collector` | Compare expected names with deployed nodes | Confirm complete gateway-fabric deployment | Every expected node exists |
| Every modeled fabric switch | Check `simple_switch_grpc` process | Confirm BMv2 dataplane availability | Process is running on every switch |
| Fabric manager and every modeled switch | Run P4Runtime manager `read`; inspect pipeline, mismatch list, LPM entries, selector groups, and members | Confirm whole-fabric control-plane programming | Read succeeds and returns exactly all modeled switches; each pipeline is ready, no mismatch exists, and each forwarding collection is non-empty |
| Every modeled client and configured Web URL | HTTP request | Confirm client-to-service forwarding | Every request returns `200` |
| `collector` | Check `python3` process | Confirm collector readiness | Process is running |
| First modeled client, first HTTP service, traversed gateway/spine/leaf, and `collector` | Use the HTTP verification traffic, read `int_reports.jsonl`, and parse the matching TCP/80 trace | Confirm INT insertion, per-hop export, collection, and trace assembly | A matching record has flow and packet IDs, a positive packet timestamp, `sink_seen=true`, `trace_complete=true`, at least three valid hops, and one gateway, spine, and leaf switch ID; every hop includes ingress/egress ports and timestamps, hop latency, queue occupancy, ECN, M, and E |

### `isp_<topology>`

Each `isp_<topology>` ID deploys one SNDlib graph with IS-IS or OSPF, optional BGP, deterministic edge traffic attachments, and the routing policy selected by its design options. Named specials (`isp_abilene_ebgp_rpki`, `isp_geant_ebgp_rpki`, `isp_abilene_ebgp_rtbh`, `isp_dfn-bwin_ebgp_rtbh`) add fixed RPKI or RTBH overlays. The Kathara FRR implementation adds a validation contract. Its contract can contain edge reachability, explicit external-prefix isolation, OSPF adjacency, BGP adjacency, BGP observer-prefix reachability, and an optional IGP metric waypoint constraint. It has no IS-IS adjacency intent type.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| All planned routers and hosts | Compare planned names with deployed nodes | Confirm SNDlib topology deployment | Every planned node exists |
| Every active IGP link | Inspect IS-IS or OSPF neighbor state | Confirm selected IGP convergence | Each planned adjacency is Up or Full |
| Planned loopbacks and inventory addresses | Router or host ping; inspect configured addresses | Confirm control-plane reachability and addressing | Required pings reply; inventory addresses are present |
| Traffic stubs, when configured | Inspect host address; ping gateway and remote stub | Confirm edge attachment and forwarding | Address exists; both probes reply |
| Planned BGP sessions and prefixes, when configured | Inspect BGP neighbor and route state | Confirm establishment, origin, and propagation | Peers establish; originated and observer prefixes appear |
| Kathara FRR BGP | Inspect BGP table for denied infrastructure prefix | Prevent forbidden infrastructure distribution | Denied prefix is absent |
| Kathara FRR RPKI named specials | Inspect RTR and RPKI route state | Confirm RPKI attachment and leak policy | RTR is connected; leak is absent |
| Kathara contract reachability/isolation intent | ICMP or TCP/UDP probe between concrete entities | Confirm required reachability or denial | Reachability connects; isolation does not connect |
| Kathara contract waypoint intent | Traceroute between concrete entities | Confirm required transit and avoidance | Trace contains required nodes and no forbidden node |
| Kathara contract BGP/OSPF adjacency intent | Inspect FRR protocol state | Confirm concrete contract adjacency | BGP peer is established or OSPF peer is Full |
| Kathara required contract report | Aggregate per-intent results | Gate startup on healthy contract | `ValidationReport.status` is `passed` |

### `min3clos`

`min3clos` provides a five-node SR Linux eBGP Clos that connects two clients through redundant fabric links.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| `leaf1`, `leaf2`, `spine1`, `client1`, `client2` | Compare expected names with deployed nodes | Confirm five-node fabric deployment | Every node exists |
| Expected leaf and client interfaces | Read interface operstate | Confirm physical topology | Every checked interface is `up` |
| `leaf1`, `leaf2` | Parse SR Linux BGP neighbors | Confirm eBGP fabric convergence | Each leaf has the required established neighbors |
| `client1`, `client2` | Ping configured default gateways | Confirm host attachment | Both gateway probes reply |
| `client1` to `client2` | ICMP ping | Confirm end-to-end fabric forwarding | Probe replies |

### `k8s_lab`

`k8s_lab` hosts a six-node k3s cluster over an FRR BGP fat-tree and exposes the Word application through an ingress virtual IP.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| Controller, five workers, and client | Compare expected names with deployed nodes | Confirm cluster topology deployment | Every node exists |
| `controller` | Inspect `eth0` IPv4 address | Confirm controller attachment | Address `201.1.1.2` exists |
| Controller to `worker3`; client to controller | Three-packet ICMP ping | Confirm fat-tree forwarding | Each probe receives three replies |
| Controller Kubernetes API | Run `kubectl get nodes --no-headers` | Confirm k3s readiness | At least six nodes report `Ready` |
| Ingress controller service | Read load-balancer ingress IP | Confirm ingress VIP allocation | Address begins with `101.` |
| `leaf_1_1` | Parse FRR BGP summary | Confirm sampled fabric BGP convergence | At least one neighbor is established |
| Client to Word app | Request live ingress VIP `/word` (hostname synced to MetalLB address) | Confirm ingress-to-workload path | HTTP returns `200` |

### `llmd_lab`

`llmd_lab` hosts a six-node k3s L2 inference cluster with MetalLB, AgentGateway, and an `llm-d` models endpoint.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| Controller, five workers, and client | Compare expected names with deployed nodes | Confirm inference-cluster deployment | Every node exists |
| Controller and client | Inspect `eth0` IPv4 addresses | Confirm endpoint addressing | Addresses `200.0.0.1` and `200.0.0.7` exist |
| Client to controller | Three-packet ICMP ping | Confirm L2 reachability | Three replies |
| Controller Kubernetes API | Run `kubectl get nodes --no-headers` | Confirm k3s readiness | At least six nodes report `Ready` |
| `metallb-system` and `agentgateway-system` | List pods | Confirm service-advertisement and gateway controllers | Both namespace outputs contain `Running` |
| `llm-d` Gateway | Read `status.addresses[0].value` | Confirm gateway service address | Address starts with `200.0.0.` |
| Client to models API | Request live Gateway VIP `/v1/models` (hostname synced to MetalLB address) | Confirm inference gateway path | HTTP returns `200` |

## ISP Batfish static validation

Batfish static validation applies only when an `isp_<topology>` scenario uses the Kathara backend, the FRR device profile, and a contract-bearing deployment. Enable it with `nika.static_validation.enabled: true` in `config/nika.yaml` or with `--static-validation` for a run.

```shell
uv sync --extra kathara --extra batfish
uv run nika env run isp_abilene --igp ospf --static-validation
```

[`snapshot.py`](../../src/nika/validation/batfish/snapshot.py) writes the exact generated FRR configuration for each ISP router into Batfish's concatenated Cumulus format. It also writes traffic hosts and `batfish/layer1_topology.json`. The adapter does not synthesize another routing configuration.

[`compiler.py`](../../src/nika/validation/batfish/compiler.py) compiles supported contract intents into counterexample searches.

| Target | Measurement | Purpose | Expected result |
| --- | --- | --- | --- |
| `reachability` source, destination, and traffic selector | Search selected flow space for a failure disposition | Prove required traffic has no failing flow | No failure flow or trace is found |
| `isolation` source, destination, and traffic selector | Search selected flow space for a success disposition | Prove denied traffic cannot forward | No successful flow or path is found |
| `waypoint` source, destination, and path constraint | Search for unreachable flow, forbidden transit, and path avoiding required nodes | Prove reachability and path compliance | No counterexample flow or path is found |
| BGP `adjacency` endpoints and parameters | Compare nodes, peer addresses, ASNs, session type, and modeled establishment | Prove planned BGP peering is compatible | Matching session is established with compatible fields |
| OSPF `adjacency` endpoints and parameters | Compare nodes, addresses, area, router IDs, and modeled establishment | Prove planned OSPF peering is compatible | Matching session is established with compatible fields |
| Entire snapshot | Parse configurations; inspect references, router IDs, and forwarding loops | Detect model-wide configuration defects | No parse failure, undefined reference, duplicate BGP/OSPF router ID, or forwarding loop |

Batfish also reports configuration parse results, undefined references, duplicate BGP or OSPF router IDs, and forwarding loops. It records an uncompileable intent as `unsupported` and keeps it in coverage. NIKA records Batfish adjacency evidence as static and model-predicted; the live FRR verifier supplies separate evidence for the same intent ID.

The pinned Batfish version does not model FRR `match rpki invalid`. An RPKI ISP run still validates adjacency intents statically, while flow and waypoint intents are `unsupported`; the Kathara runtime verifier checks deployed RPKI behavior.

## Validation contract schema

[`contract.py`](../../src/nika/net_env/contract.py) defines Pydantic models that reject unknown fields and serialize as JSON.

| Model | Purpose |
| --- | --- |
| `ValidationContract` | Stable contract ID, scenario, design source, and sorted intents |
| `ValidationIntent` | Stable ID, description, property, expected state, level, concrete entities, and property-specific data |
| `NetworkEntity` | Concrete node, endpoint, prefix, or service with optional address and hosting node |
| `TrafficSelector` | IPv4 protocol; TCP and UDP require a destination port |
| `PathConstraint` | Nodes that traffic must traverse or avoid |
| `AdjacencyExpectation` | BGP or OSPF endpoints and protocol-specific fields |
| `ValidationResult` | Intent ID, verifier, support, status, evidence, reason, and duration |
| `ValidationReport` | Aggregate status, coverage, sanity checks, metadata, and one result per intent |

| Property | Expected state | Required fields |
| --- | --- | --- |
| `reachability` | `reachable` | source, destination, traffic |
| `isolation` | `unreachable` | source, destination, traffic |
| `waypoint` | `path_compliant` | source, destination, traffic, path |
| `adjacency` | `established` | adjacency |

Use `required` when a failed intent must fail startup. An `optional` intent records its result but does not change a passing aggregate report. Resolve `EntitySelector` groups during scenario construction through `SelectorCatalog`, store concrete `NetworkEntity` values in final intents, sort by stable ID, and omit timestamps or runtime-generated IDs.

## Failure-effect validation

Failure-effect validation compares the healthy contract with the lab after a real injection. The injection supplies the post-injection FRR configuration to the faulty Batfish snapshot. NIKA does not synthesize a fault configuration.

| Status | Meaning |
| --- | --- |
| `PASS` | Static and runtime validation observed each declared change and preserved property |
| `FAIL` | The healthy baseline failed, an expected change did not occur, or a preserved property failed |
| `STATIC_RUNTIME_MISMATCH` | Static and runtime validation observed different states |
| `UNSUPPORTED` | The failure lacks a declared effect or a required verifier report |

## Add or change a runtime validation

1. Define the healthy behavior from the scenario's topology model, plan, workload, and policy. Do not derive it from the result of the command that checks it.
2. Add the check in the scenario's `verify.py`. Use `LabRuntime` and helpers from [`net_env/verify.py`](../../src/nika/net_env/verify.py) for node presence, probes, process state, routes, or HTTP. Add a local helper for protocol-specific parsing.
3. Generate checks from the model or plan for scalable scenarios. Fixed sample checks suit readiness signals; policy invariants such as VRF isolation should iterate over designed entities.
4. Return `build_lab_verify_result(scenario_name=..., verified=all(checks.values()), checks=checks, details=...)`. Keep checks boolean and stable for startup logs and tests. Put diagnostic observations in `details`.
5. Ensure `verify_lab()` passes its workload, model, plan, topology size, or other design inputs to the verifier. `verify_lab_with_retry()` polls the method using `VERIFY_MAX_WAIT_SEC` and `VERIFY_RETRY_DELAY_SEC` when the scenario defines them.
6. Add focused tests for a passing result and each new false condition. Use the scenario's existing verifier test module when present. Run unit tests before Docker, Kathara, Containerlab, Kubernetes, or P4 integration coverage.

## Add a contract or Batfish validation

1. Add `get_validation_contract()` only when the healthy behavior fits concrete IPv4 reachability, isolation, waypoint, BGP adjacency, or OSPF adjacency. Build intent IDs and `design_source` from stable design inputs.
2. Implement live evaluation of the same intents and write one `ValidationResult` per intent. Preserve each intent's expected state and level. Build reports with `ValidationReport.from_results()`; missing, duplicate, unknown, or mixed-verifier results are invalid.
3. Add a snapshot adapter that writes exact deployed configuration and required host and Layer 1 inputs. Extend static-validation support gating only after the adapter accepts the scenario's backend and device profile.
4. Extend the Batfish compiler and client for a supported property. If Batfish cannot compile or model an intent, return `supported=false` and `status="unsupported"` with a reason rather than claiming static coverage.
5. Test schema validation, stable contract generation, runtime evidence, compiler questions, snapshot contents, and verifier coverage. Run live Batfish tests only when the Batfish extra and Docker are available.

```shell
uv run pytest -q tests/nika/net_env/test_validation_contract.py tests/nika/net_env/isp/test_isp_contract.py tests/nika/net_env/isp/test_isp_verify_unit.py
uv run pytest -q tests/nika/validation -k 'not live'
```
