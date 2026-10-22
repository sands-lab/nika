# Specification-driven environment verification

NIKA verifies routing, SDN and P4 environments against explicit behavior specifications. Each experiment retains the original topology and design choices, constructs a candidate configuration, then checks it with independent static analysis and native execution. Routing experiments can complete configuration parameters with NetComplete before generation. Acceptance requires the candidate to satisfy the original requirements within the documented model and execution scopes.

Run this workflow explicitly during environment development. Normal benchmark
execution keeps its existing construction and startup checks. Developers can
replay an accepted construction through the startup API; benchmark runs do not
select saved constructions or invoke the synthesis and verification tools by default.

## Construction and acceptance

```text
Original topology + authored behavior + fixed domain design
                         |
             Domain-specific configuration construction
          (parameter completion where applicable)
                         |
       Batfish or KATch checks of original behavioral predicates
                         |
       Native checks of the same predicates and runtime properties
                         |
          Declared fault effects and complete recovery
```

1. **Specify behavior.** Typed requirements name permitted paths, exact ECMP sets, BGP candidate order, adjacencies and communication or isolation. Requirements and incident expectations precede synthesis. Fixed addressing and session bindings resolve them to concrete `ValidationIntent` predicates.
2. **Complete supported parameters.** For routing experiments, [NetComplete's partial-synthesis formulation](https://netcomplete.ethz.ch/files/netcomplete-nsdi2018.pdf) supplies the method: retain a routing sketch and solve its unknown configuration values. NIKA delegates supported path costs, export decisions and import preferences to the pinned upstream implementation. Solver output cannot redefine the requirements. Fixed SDN and P4 forwarding candidates do not need this completion step.
3. **Generate configurations.** Bind the fixed design and any completed parameters to the original graph, protocols and services. The optional netlab construction path delegates routing topology transformation and vendor syntax to upstream netlab. SDN and P4 experiments retain their existing domain generators. NIKA retains topology, services and the documented features outside the upstream templates. Generation supplies a candidate; independent checks establish acceptance.
4. **Check the candidate independently.** Batfish analyzes routing configurations or restricted projections; KATch checks the supported OpenFlow and P4 forwarding models. Both evaluate the original behavioral predicates. Required errors, unsupported results and violations reject the candidate. Counterexamples must violate their independently declared predicate sets.
5. **Check execution.** Native protocol, route and flow observations evaluate the same requirements. Runtime-only requirements cover properties outside the static model, including the evaluated FRR ECMP installation limit and enterprise overlay behavior.
6. **Validate the incident.** Exercise declared faults through NIKA's failure or runtime APIs. Modeled configuration faults must reproduce the static analyzer's predicted violation set. Recovery must restore the complete original healthy requirements.

Acceptance means the candidate configuration satisfies the declared properties in the supported static model and the exercised runtime states. The method does not establish universal compiler correctness, equivalence between an analyzer and a device implementation, or global protocol convergence under arbitrary failures. Hidden implementation bugs remain possible.

## Predicates and established methods

NIKA's typed predicates are input to existing analyses, rather than a new formal logic. For example, an AS exit requirement becomes a predicate on the selected BGP route:

```text
selected_route(router, prefix).first_external_as = required_neighbor_as
```

An exact next-hop requirement has the form:

```text
installed_next_hops(router, prefix) = permitted_next_hops
```

The original graph and path specification determine the permitted set before completion. Batfish evaluates that set through its routing model; the native verifier compares it with installed routes. ICMP requirements can declare `round_trip: true`, requiring both forward delivery and a successful return path.

For link-state completion, the specification supplies the exact shortest-path set, including ECMP, for each declared surviving graph:

```text
P(s,d) = argmin over paths p from s to d of sum(metric(e) for e in p)
```

The adapter lowers these requirements to NetComplete's `PathReq` and `ECMPPathsReq`. Healthy and fault-state graph copies share surviving-link metric variables. Contradictory state requirements must therefore return UNSAT. IPv4 single-level IS-IS uses the same additive shortest-path objective under an explicit reduction, with symmetric metrics limited to 1–65,535. [RFC 1195](https://www.rfc-editor.org/rfc/rfc1195) and [RFC 5305](https://www.rfc-editor.org/rfc/rfc5305) define the relevant IS-IS semantics; this reduction does not claim native NetComplete IS-IS synthesis.

The [experiment guide](../../experiment/scenario_validation/README.md#tool-responsibilities-and-model-scope) states each synthesis and verifier boundary. Unsupported forwarding syntax fails explicitly. The implementation keeps fixed security policies outside the completed BGP scope.

## An Abilene example

The original benchmark profile selects Abilene, FRR, OSPF and eBGP. The experiment adds an authored exit order for an original business prefix:

```python
from pathlib import Path
from nika.net_env.synthesis.requirements import BgpAsSelectionRequirement
from nika.workflows.validation.benchmark_routing import (
    benchmark_isp_profiles, build_benchmark_isp_experiment,
)
from nika.workflows.validation.scenario import validate_scenario_design

profile = benchmark_isp_profiles([Path("benchmark/working/cases.yaml")])[
    "isp_abilene--ospf--ebgp--kathara"
]
selection = BgpAsSelectionRequirement(
    kind="bgp_as_selection", id="exit.as65001.prefix102",
    asn=65001, prefix="198.51.102.0/24", candidates=(65002, 65003),
)
spec = build_benchmark_isp_experiment(profile, bgp_selections=(selection,))
summary = validate_scenario_design(spec, Path("/tmp/abilene-policy"), runtime=True)
```

The builder composes IGP and AS import completion. Before generation, requirement resolution fixes predicates such as:

```json
{
  "id": "exit.as65001.prefix102.hstnng",
  "property": "bgp_route",
  "bgp_route": {
    "node": "hstnng",
    "prefix": "198.51.102.0/24",
    "exit_asn": 65002,
    "present": true
  }
}
```

A recorded execution completed 20 interface costs and seven neighbor import policies. For this prefix, NetComplete assigned local preference 2 at the AS65002 ingresses and 1 at the AS65003 ingress. The FRR renderer produced terms including:

```text
ip prefix-list NC-IN-0-P2 permit 198.51.102.0/24
route-map NC-IN-0 permit 20
 match ip address prefix-list NC-IN-0-P2
 set local-preference 2
 call BGP-IN
```

Both Batfish and native BGP observations selected AS path `65002 65003`, satisfying the original exit requirement. The numeric preferences are solver outputs; the specification requires AS65002, independently of those values.

The retained Abilene regression includes three original exit selections. Its recorded checks passed 122 healthy predicates, reproduced the two directed BGP-session violations from the selected maximum-prefix fault, and reproduced the nine original violations predicted for a business-advertisement withdrawal. The declared backbone-link state passed its 122 condition-specific predicates. Each recovery restored all 122 healthy predicates. Reproduce the complete example with:

```bash
uv run pytest 'tests/nika/validation/test_benchmark_routing.py::test_as_import_and_igp_completion_preserve_benchmark_failures[isp_abilene-kathara]' -q
```

## Reuse and coverage

Reusable completion code lives in `src/nika/net_env/synthesis`; verifier adapters live in `src/nika/validation`; construction and validation orchestration live in `src/nika/workflows/validation`. Manual entry points and specimens live in `experiment/scenario_validation`. Normal benchmark execution does not invoke the construction experiment. Saved original deployment choices and completed parameters can be loaded explicitly through `load_scenario_build` and the existing startup API.

Routing and packet-fabric regressions call the same `validate_scenario_design`
entry point and share
[`assert_scenario_acceptance`](../../tests/support/scenario_validation.py).
That assertion checks declared predicate IDs in saved reports, healthy and fault
states, configuration-counterexample rejection, healthy-state recovery and session
closure. Routing resolution must retain each original predicate unchanged.
Scenario differences enter through specification builders and parameter matrices;
protocol-specific assertions supplement the common acceptance criteria.

Use the benchmark inventory runner and retained test matrices to reproduce
coverage on the current revision. Compare the complete topology, device profile,
IGP and BGP mode: offline compilation, static analysis and native acceptance are
distinct results. Each experiment saves its evidence in the selected output
directory. The native ISP regressions also cover Cisco XRd and RouterOS beyond
their original two-router direct-eBGP examples.

The workflow covers declared properties rather than every function of an
environment. The following boundaries also apply to the experiment inventory:

| Environment family | Specification-driven checks | Remaining execution or coverage boundary |
| --- | --- | --- |
| ISP and direct BGP | Original adjacencies, routes, next hops, routing policy and endpoint communication where declared | Native acceptance covers recorded deployment profiles, not every graph/vendor/protocol combination |
| Clos, campus and min3clos | Declared routing, communication and isolation predicates | Installed ECMP limits require native observations; retained test matrices declare the exercised sizes |
| Enterprise | Static-underlay communication and declared routing predicates | WireGuard, VRF routing and isolation require native observations |
| SDN and P4 fabrics | Client-service ICMP and unowned-address isolation, configuration counterexamples and endpoint outages | Recorded native acceptance covers sizes `s` and `m`; P4 stateful services, INT and ECN remain runtime checks |
| Kubernetes and llmd | Existing readiness and runtime checks only | No integration with this specification-driven experiment and no formal correctness claim |

Kubernetes runtime checks cover cluster readiness, declared BGP peers, endpoint
communication and application HTTP behavior. llmd checks cover cluster and serving
readiness, endpoint communication and the models HTTP endpoint. Those observations
remain useful execution evidence; they do not imply that NetComplete, Batfish or
NetKAT analyzed Kubernetes policies or application behavior.

The additional packet-fabric scale experiments recorded the following static
results. A passed row includes healthy requirements, declared endpoint-outage
requirements and the exact predeclared configuration-counterexample violation set.
Native acceptance also covers sizes `s` and `m`.
Size `l` has static acceptance only.

| Environment | Size | Healthy / outage predicates | Complete static acceptance |
| --- | --- | --- | --- |
| SDN Clos | m | 416 / 416 | Passed; 48 predicted counterexample violations |
| P4 fabric | m | 416 / 416 | Passed; 48 predicted counterexample violations |
| P4 gateway | m | 76 / 76 | Passed; 16 predicted counterexample violations |
| SDN Clos | l | 1,600 / 1,600 | Passed; 96 predicted counterexample violations |
| P4 fabric | l | 1,600 / 1,600 | Passed; 96 predicted counterexample violations |
| P4 gateway | l | 280 / 280 | Passed; 32 predicted counterexample violations |

The batch driver loads the network once per analysis and invokes KATch's upstream
parser and equivalence procedure for each original assertion in one JVM. Normal
assertion failures remain predicate violations and allow subsequent assertions to
run. Parser errors, resource failures and incomplete query results reject
acceptance. The large SDN and P4 fabric counterexamples completed in approximately
16 and 15 seconds respectively, within the original 300-second analysis budget and
the unchanged 2 GiB memory and two-CPU limits.

A new compatible topology supplies different inputs to this workflow. A new protocol or vendor needs supported completion bindings, configuration analysis and native observations while preserving the original requirements. Cross-domain reuse retains this construction and acceptance sequence and states the remaining modeling limits.

## ISP device profiles

`device_profile` selects a concrete network operating system, while `backend`
selects its execution environment. The existing benchmark row identity includes
both fields, so the same graph and protocol sketch can have distinct FRR, SR
Linux, IOS XR and RouterOS tasks without adding a separate `vendor` field.
IOS XR and RouterOS ISP deployments support `igp: ospf` or `isis` (Level 2) and
`bgp_mode: none`, `ibgp_rr` and `ebgp`; security overlays remain excluded. ISP
contracts declare OSPF adjacencies only, so readiness checks IS-IS Level-2
adjacencies natively on both. RouterOS IS-IS analysis projects the concrete
RouterOS circuits to the same IOS XR form used for FRR IS-IS.

With both vendor images installed, run the retained native matrix serially:

```bash
uv run pytest tests/nika/validation/test_vendor_isp_workflow.py -m e2e -x -q
```

The matrix uses ARPANET196912 for OSPF and iBGP route reflection, and DataXchange
for multi-AS BGP. IOS XR runs each of these BGP sketches over both OSPF and
IS-IS, and RouterOS runs all three BGP sketches over IS-IS. Each profile follows NetComplete, Batfish, native checks,
configuration negative controls, link failure and recovery. Keep native
acceptance results separate from offline configuration compilation.

Recorded native examples include:

| Graph | BGP sketch | Profiles | Healthy predicates |
| --- | --- | --- | --- |
| ARPANET196912 | none | IOS XR (OSPF, IS-IS), RouterOS | 20 |
| ARPANET196912 | iBGP route reflection | IOS XR (OSPF, IS-IS), RouterOS | 36 |
| DataXchange | multi-AS BGP | IOS XR (OSPF, IS-IS), RouterOS | 62 |
| Abilene | multi-AS BGP | RouterOS | 133 |

The `-iosxr` Clos, campus and enterprise specimens (size `s`) also passed native
execution with 14 + 2, 21 and 4 + 6 healthy predicates, including their registered
failures on XR or FRR routers.

Each listed execution also rejected its declared configuration negative controls,
checked the link-failure state and restored every healthy predicate. These are
model-specific acceptance results, not a proof about arbitrary configurations.
The IOS XR Abilene variant passed its 133 static checks, but its native deployment
was rejected by memory admission and is not included in native coverage.

To generate fault targets for a profile, use the existing benchmark generator:

```python
from nika.workflows.benchmark.generate import build_failure_cases

cases, rejected = build_failure_cases(
    scenario="isp_arpanet196912", problem="link_down", topo_size="",
    isp_options={"igp": "ospf", "bgp_mode": "none", "rpki": False,
                 "backend": "kathara", "device_profile": "routeros"},
)
rows = [dict(scenario="isp_arpanet196912", problem="link_down", **case)
        for case in cases]
```

The generated rows retain `device_profile`, injection targets and ground truth.
A manifest containing multiple profiles is handled by `benchmark_isp_profiles`
and the existing routing experiment. Native variants receive distinct profile
keys; existing default-profile keys remain unchanged.

The native profiles consume the same typed graph, path requirements and
NetComplete parameter types. IOS XR configurations go directly to Batfish.
RouterOS configurations pass through a restricted, concrete-command projection:
explicit IPv4 addresses, point-to-point OSPF in area 0, passive interfaces,
BGP sessions and reflector roles, exact-prefix filters and local preference.
RouterOS IS-IS becomes the FRR Level-2 wide-metric form and then IOS XR, as FRR
IS-IS does: point-to-point circuits keep their Level-2 metric, and passive
circuits, including the loopback, advertise their prefix with metric 0, as
RouterOS does.
The projection reads the final candidate, including counterexamples; unknown
commands or policy actions are rejected. Native forwarding and protocol
observations remain required to validate this model's assumptions.

Native ISP profiles use standard next-hop selection: reflected routes retain their
next hop, as recommended by [RFC 4456 section 4](https://www.rfc-editor.org/rfc/rfc4456#section-4).
External-link subnets are advertised passively in the AS's OSPF domain, keeping
unchanged external next hops reachable. RouterOS `force-self` also rewrites
reflected next hops; it is not equivalent to ordinary FRR `next-hop-self`.
The restricted projection rejects that iBGP setting rather than modeling it
as FRR's different operation. See the
[RouterOS next-hop choices](https://help.mikrotik.com/docs/spaces/ROS/pages/331612228/routing%2Bbgp)
and [FRR BGP commands](https://docs.frrouting.org/en/latest/bgp.html).

For a vendor comparison, regenerate injection targets from each profile's
inventory. RouterOS reserves its first port for management, so copying a Linux
interface index from an FRR task can select a different attachment. Pair tasks
by the original graph edge and fault, then retain the generated physical
attachment and ground truth for each profile. The routing experiment accepts
native `link_down` bindings and explicitly rejects configuration failures that
still require FRR-specific commands. Published benchmark releases are unchanged.

Native ISP profiles use the benchmark runner's heavy tier, which runs exclusively
by default. Before creating containers, deployment checks a conservative memory
estimate: 3 GiB per XRd router or 1 GiB per RouterOS router, 256 MiB per edge host,
and 2 GiB of headroom. Insufficient available memory rejects deployment. These
estimates limit experiment size. Native router containers also have matching
3 GiB / 1 GiB memory limits and one CPU each.
Large graphs can be compiled offline without claiming that native execution was
validated.

In ISP environments, one `device_profile` applies to every router. In `dc_clos`,
`campus_lan` and `enterprise_branch`, `device_profile: iosxr` runs only the routers
of the eligible roles on XR, and the other nodes keep FRR/Linux. These experiments
complete the FRR sketch, translate each eligible router's completed configuration
to IOS XR, and analyze the XR and FRR configurations together in one Batfish
snapshot. The `-iosxr` specimens in `experiment/scenario_validation` retain the FRR
specimens' requirements and replace only the XR-specific negative controls.

### RouterOS routers in Ethernet environments

`dc_clos`, `campus_lan` and `enterprise_branch` at size `s` run their eligible
router roles on RouterOS with `device_profile: routeros`. The specimens
`clos-routeros.json`, `campus-routeros.json` and `enterprise-routeros.json`
keep the FRR specimens' original requirements, completion sketches, incidents
and registered failures; counterexamples use RouterOS commands.

NetComplete completes the FRR sketch. The RouterOS translation keeps the
solved values: export route maps become filter chains that apply the first
exact-prefix match and reject the rest, OSPF costs set the network template's
cost, and Site Edge import preferences become the tunnel's import chain.
Batfish analyzes the final RouterOS candidates together with the FRR routers.
Each converted router is projected back to FRR under its container interface
names: addresses, static and blackhole routes, OSPF network templates with
their costs (RouterOS default 1) and passive flags, and BGP with connected
origins and exact-prefix chains. Enterprise Site Edges, analyzed with the Linux
static model, contribute only their `main` table, as FRR edges do; VRFs,
WireGuard, BGP, NAT and QoS stay runtime requirements. The projections reject
any other statement.

Native checks observe intents on the router that holds them, including
non-backbone OSPF areas and the Site Edge's overlay BGP table. After a
registered failure, restoration re-adds only the RouterOS menu groups whose
configuration changed. `ospf_area_misconfiguration` moves the router's first
area templates to a new area natively.

RouterOS installs one best BGP path, so the Clos specimen omits the FRR
specimen's healthy ECMP predicates and its `maximum-paths` counterexample; it
disables one BGP session natively instead. The RouterOS platform declares no
netlab device for these environments, so `--config-generator netlab` rejects
them; netlab's RouterOS device also has no IS-IS module, so RouterOS IS-IS ISP
profiles use NIKA's renderer only.

## Delegate configuration generation to netlab

The manual construction workflow can use pinned [netlab](https://netlab.tools/netlab/create/)
to transform the completed topology into vendor configurations:

```bash
uv sync --extra batfish --extra netlab
```

```python
from copy import deepcopy
from pathlib import Path
from nika.workflows.validation.benchmark_routing import (
    isp_catalog_profiles, build_benchmark_isp_experiment,
)
from nika.workflows.validation.scenario import validate_scenario_design

profile = deepcopy(next(
    p for p in isp_catalog_profiles().values() if p["scenario"] == "isp_dataxchange"
))
profile["deployment"].update(igp="ospf", bgp_mode="ebgp", device_profile="frr")
spec = build_benchmark_isp_experiment(profile)
summary = validate_scenario_design(
    spec, Path("/tmp/isp-netlab"), runtime=True, config_generator="netlab",
)
```

NetComplete still selects IGP costs and prefix import preferences from the original
behavior requirements. A shared adapter supplies explicit addresses, directed
costs, AS membership, RR roles, connected business prefixes and policies to
netlab. Upstream topology transformation derives the BGP sessions, and upstream
interface, IGP, BGP and routing-policy templates generate the device commands.
A derived session set that differs from the fixed sketch is rejected.

The same construction path covers these existing routing sketches:

| Sketch | Configuration generation | Retained NIKA responsibilities |
| --- | --- | --- |
| ISP FRR and SR Linux | OSPF or Level-2 IS-IS; no BGP, iBGP RR or ordinary eBGP | Fixed topology and protocol choices; SR Linux IGP ECMP capacity |
| ISP IOS XR | OSPF or Level-2 IS-IS; no BGP, iBGP RR or eBGP | None beyond the fixed sketch |
| ISP RouterOS | OSPF; no BGP, iBGP RR or eBGP (netlab has no RouterOS IS-IS) | RouterOS exact-prefix filters and local preference |
| Clos and campus FRR | Ethernet OSPF or eBGP, including solved export policies | Linux addressing, switching and services |
| Enterprise FRR | Tunnel-endpoint BGP and prefix/preference policies | WireGuard lifecycle and existing default-to-business VRF imports |
| Clos, campus and enterprise with IOS XR routers | The same sketches, with eligible routers generated as netlab `iosxr` devices | Static routes, eBGP multipath and open eBGP policies on the XR routers |
| SR Linux BGP fabric | IPv4 BGP and solved export policies | Existing IPv6 interface addressing |
| Direct IOS XR and RouterOS BGP | Fixed addressed peers and connected origins | RouterOS exact-prefix export filters |
| FRR OSPF/eBGP security profiles | Base interfaces, protocols, origins and ordinary policies | Existing RPKI/RTBH policy fragments and runtime attachments |

NetComplete supplies the solved values; netlab supplies protocol and policy syntax.
Prefix filters and local preference use upstream's [routing module](https://netlab.tools/module/routing/)
and [BGP policy plugin](https://netlab.tools/plugins/bgp.policy/). The latter binds
eBGP policies only. NIKA attaches the same upstream-generated admission policies
to the actual iBGP peers in the fixed ISP sketch, as its native renderers do. In
the FRR eBGP sketch, iBGP peers keep only the import policy and use next-hop-self,
so leak prefixes still reach the AS borders. RouterOS lacks this generic
policy module, so NIKA retains its existing finite filter implementation.
The summary records these retained configuration responsibilities. Unsupported
protocol or policy combinations raise an error.

The pinned RouterOS templates need bounded compatibility corrections for
community property names, the loopback source address and additional loopback
settings. The initial template creates these loopbacks as bridges, so the runner
binds their settings to `/interface/bridge` rather than `/interface/ethernet`.
The runner applies the names documented in
[RouterOS Route Selection and Filters](https://help.mikrotik.com/docs/spaces/ROS/pages/74678285/Route%2BSelection%2Band%2BFilters)
and binds the source to the explicit loopback address. FRR policies retain the
existing incident-facing names; maximum-prefix injection selects the policies
actually attached to peers, including solved import policies.

The construction saves the input topology, transformed model, upstream fragments
and final candidate configurations. Batfish evaluates the final candidates against
the original predicates. Native execution deploys those same candidates through
NIKA's ordinary lifecycle and checks the original behavior, declared negative
controls, outages and recovery. Bindings select the corresponding command or
configuration object in the new syntax; they preserve the original violation sets.
The saved-build loader reads the accepted candidates without invoking the tools.

The static models remain explicit about their scope:

- FRR OSPF/BGP and IOS XR use Batfish's native parsers. FRR passive-interface
  normalization follows [the FRR OSPF command semantics](https://docs.frrouting.org/en/stable-10.2/ospfd.html).
- FRR IS-IS uses a restricted concrete IPv4 Level-2 projection to IOS XR. Its BGP
  projection preserves origins, source interfaces, ASNs, RR roles, prefix admission
  and local preference. The eBGP subset also preserves policy defaults and AS-path
  multipath eligibility from the [FRR BGP commands](https://docs.frrouting.org/en/latest/bgp.html).
- SR Linux analysis expands concrete group inheritance and ordered policies from
  the generated configuration tree. Default values follow the pinned
  [SR Linux 24.10 YANG models](https://github.com/nokia/srlinux-yang-models/tree/v24.10.1/srlinux-yang-models/srl_nokia/models).
  NIKA assembles upstream path/value operations in order and rejects unsupported
  operations. Both native execution and static projection consume that assembled tree.
- RouterOS analysis resolves actual templates, connection updates and finite prefix
  filters according to the [RouterOS BGP configuration model](https://help.mikrotik.com/docs/spaces/ROS/pages/328220/BGP).
  It excludes community housekeeping from the IPv4 route model; it does not claim
  verification of general community policy.

Unknown routing attributes fail the restricted projections. Enterprise business
VRF forwarding and WireGuard behavior remain native obligations. IPv6 routing,
arbitrary redistribution, overlays and global convergence are outside these models.
The pinned SR Linux dual-stack BGP template repeats a YAML address-family key;
this IPv4 workflow supplies IPv4 routing inputs and retains the original IPv6
interface data. It rejects duplicate YAML fields rather than dropping one family.

This delegates configuration transformation and syntax to an established tool;
it does not add a compiler-correctness claim. Acceptance continues to come from
independent checks of the original requirements. The default benchmark path and
published case definitions do not change.

## SDN and P4 forwarding verification

SDN and P4 experiments use the same `ScenarioExperiment`, `ValidationIntent`,
`ValidationSnapshot`, dispatcher and report types as the routing experiments.
`routing_model="netkat"` selects the packet-forwarding backend through the existing
`validate_scenario_design` API. Normal benchmark startup keeps its existing behavior.

```text
Authored communication policy + original endpoint attachments
                         |
       Existing OpenFlow/P4Runtime configuration generator
                         |
       Restricted match/action model + physical topology
                         |
            NetKAT queries checked by upstream KATch
                         |
       Native observations of the original predicate IDs
                         |
       Configuration counterexample + endpoint outage + recovery
```

The shared example policy requires client-service ICMP communication in both
directions and isolation from an unowned test address. Endpoint roles and
attachments determine this specification before configuration generation. It does
not assume arbitrary endpoint full-mesh communication: the gateway design provides
client-service forwarding, while additional same-role communication needs its own
specification. You can also author a `ScenarioExperiment` with explicit packet
requirements instead of using the example policy builder.

| Scenario capability | Concrete candidate | Static interpretation |
| --- | --- | --- |
| OpenFlow Clos | Existing `build_forwarding_rules` output | Single-table IPv4 destination matches, explicit priorities, MAC rewrites, output ports and SELECT groups |
| P4Runtime Clos | Existing `build_forwarding_intent` output and compiled `fabric.p4` | Destination LPM entries, selector groups/members and the checked forwarding action |
| P4Runtime gateway fabric | Existing `build_gateway_intent` output and compiled `gateway.p4` | The same ICMP forwarding slice; TCP VIP and stateful functions remain native obligations |

Frontends dispatch on topology-model capabilities, so topology size does not
change the analysis procedure. NIKA retains the existing domain configuration
generators. These fixed forwarding candidates have no routing-parameter holes;
NetComplete and netlab do not participate in their construction.

### Method and supported properties

[Header Space Analysis](https://www.usenix.org/conference/nsdi12/technical-sessions/presentation/kazemian)
models networks through composed packet-header transformations.
[NetKAT](https://doi.org/10.1145/2535838.2535862) provides an algebraic semantics,
and [MatchKAT](https://arxiv.org/abs/2102.07374) relates match/action processing to
NetKAT. NIKA lowers the supported forwarding slice to that language and delegates
its equivalence queries to [KATch](https://github.com/cornell-netlab/KATch).
NIKA does not implement a new reachability solver.

Let `F` be one forwarding step, `I` the original input packet class and `O` the
specified receiving endpoint. The reachability query is:

```text
I ; F* ; O != 0
```

Isolation requires the corresponding relation to equal zero. Round-trip delivery
requires a second reachability query with the original endpoints reversed.
Static waypoint requirements exclude successful traces that avoid a required node
or visit a forbidden node. Native waypoint identity mapping is not supported yet.
Required unsupported properties and tool errors prevent acceptance.

The OpenFlow frontend subtracts higher-priority prefix regions before encoding
lower-priority rules. The P4Runtime frontend resolves actual numeric output ports
and member/group references; descriptive peer labels do not determine forwarding.
The shared topology model supplies the physical attachments. Endpoint reception
checks the rewritten destination MAC. Unsupported IPv4 OpenFlow actions, ambiguous
priorities and unresolved P4 members or ports reject the construction.

Selector choices overapproximate possible hash outputs. Consequently, isolation
and forbidden-path checks quantify over the modeled choices, while reachability
establishes a possible path. It does not establish delivery for every hash choice,
uniform load balance, absence of loops or global convergence. Inputs assume
well-formed, unfragmented IPv4 ICMP, sufficient TTL and resolved neighbors. The
static model excludes ARP, controller transitions, queue behavior and registers.

### P4 program boundary

A P4 configuration includes both its program and table entries. The experiment
compiles the original program with the installed BMv2 p4c image and retains its
JSON output. Fixed checks inspect the destination LPM key and the forwarding
action's assignments to the output port, Ethernet addresses and TTL, following the
[BMv2 JSON representation](https://github.com/p4lang/behavioral-model/blob/main/docs/JSON_format.md).
A changed action outside that slice rejects the projection.

These checks do not establish full parser, control-flow, checksum or extern
correctness. The model assumes that the declared ICMP forwarding path reaches the
checked LPM/action slice. Native execution tests that assumption for the original
requirements. Gateway TCP load balancing, INT, ECN and multi-packet state continue
to rely on the scenario's existing native startup verification; the experiment
does not label those functions as NetKAT-verified.

[P4Testgen](https://p4lang.github.io/p4c/p4testgen.html) and
[p4v](https://www.cs.cornell.edu/~jnfoster/papers/p4v.pdf) provide complementary
program-level approaches. Neither is integrated into this workflow. P4Testgen
symbolically generates single-packet tests; bounded test success would add testing
evidence, rather than a proof of the original network specification.

### Counterexamples and operating states

Before modifying a candidate, the experiment declares which original round-trip
predicates depend on one selected endpoint. It removes that endpoint's P4 LPM
entry, or installs a higher-priority OpenFlow drop for the same destination.
KATch must reject exactly the declared predicates. Applying the corresponding
change through P4Runtime or OpenFlow must reproduce that violation set; restoring
the candidate must satisfy the complete healthy specification.
The OpenFlow experiment uses the existing ONOS flow compiler and controller API
for installation and removal. Controller-owned rules preserve the counterexample
throughout the predicate scan; unmanaged OVS rules can disappear during controller
reconciliation. This procedure applies across topology sizes.

The endpoint-outage model removes one attachment. Native execution administratively
disables only the endpoint's own interface, checks the declared fault-state
requirements, then restores the interface and all healthy requirements. This
experiment leaves the benchmark's registered faults unchanged. The broader
benchmark `link_down` action also disables the switch-side interface; an initial
BMv2 experiment showed additional interruption on another endpoint of that leaf.
That run failed acceptance and provides no evidence for the broader fault model.

KATch containers use a 2 GiB memory limit, two CPUs and a total analysis timeout.
P4 compilation uses the same resource bounds. Experiments retain the original
specification, candidate, restricted model, concrete NetKAT queries, tool logs and
static/native reports. Run native environments sequentially. Consult the
[experiment guide](../../experiment/scenario_validation/README.md#verify-sdn-and-p4-fabrics)
for the manual entrypoint and retained regressions.
