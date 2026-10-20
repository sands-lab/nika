# Specification-driven routing construction

NIKA prepares routing environments from explicit behavior and a partial network design. A topology graph supplies structure; the experiment specifies protocols, route-reflector roles, prefix ownership and intended routing behavior. A converged configuration can implement the wrong policy, so acceptance requires both independent configuration analysis and native execution against the original requirements.

Run this workflow explicitly during environment development. Normal benchmark
execution keeps its existing construction and startup checks. Developers can
replay an accepted construction through the startup API; benchmark runs do not
select saved constructions or invoke the synthesis and verification tools by default.

## Construction and acceptance

```text
Public graph + authored behavior + fixed routing sketch
                         |
             NetComplete parameter completion
                         |
       netlab templates or existing NIKA generators
                         |
        Batfish checks of original behavioral predicates
                         |
       Native checks of the same predicates and runtime properties
                         |
          Declared fault effects and complete recovery
```

1. **Specify behavior.** Typed requirements name permitted paths, exact ECMP sets, BGP candidate order, adjacencies and communication or isolation. Requirements and incident expectations precede synthesis. Fixed addressing and session bindings resolve them to concrete `ValidationIntent` predicates.
2. **Complete supported parameters.** [NetComplete's partial-synthesis formulation](https://netcomplete.ethz.ch/files/netcomplete-nsdi2018.pdf) supplies the method: retain a routing sketch and solve its unknown configuration values. NIKA delegates supported path costs, export decisions and import preferences to the pinned upstream implementation. Solver output cannot redefine the requirements.
3. **Generate configurations.** Bind the completed parameters to the original graph, protocols and services. The optional netlab construction path delegates topology transformation and vendor syntax to upstream netlab. NIKA retains topology, services and the documented features outside the upstream templates. Generation supplies a candidate; independent checks establish acceptance.
4. **Check the candidate independently.** Batfish parses the final configuration or an explicitly restricted projection and evaluates the original predicates. Required errors, unsupported results and violations reject the candidate. Counterexamples must violate their independently declared predicate sets.
5. **Check execution.** Native protocol, route and flow observations evaluate the same requirements. Runtime-only requirements cover properties outside the static model, including the evaluated FRR ECMP installation limit and enterprise overlay behavior.
6. **Validate the incident.** Normal NIKA failure injection must produce its declared effects. Modeled configuration faults must also reproduce Batfish's predicted violation set. Recovery must restore the complete original healthy requirements.

Acceptance means the completed configuration satisfies the declared properties in the supported static model and the exercised runtime states. The method does not establish universal compiler correctness, equivalence between Batfish and a router implementation, or global protocol convergence under arbitrary failures. Hidden implementation bugs remain possible.

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

The [experiment guide](../../experiment/routing_validation/README.md#tool-responsibilities-and-model-scope) states each synthesis and verifier boundary. Unsupported forwarding syntax fails explicitly. The implementation keeps fixed security policies outside the completed BGP scope.

## An Abilene example

The original benchmark profile selects Abilene, FRR, OSPF and eBGP. The experiment adds an authored exit order for an original business prefix:

```python
from pathlib import Path
from nika.net_env.synthesis.requirements import BgpAsSelectionRequirement
from nika.workflows.validation.benchmark_routing import (
    benchmark_isp_profiles, build_benchmark_isp_experiment,
)
from nika.workflows.validation.routing import validate_routing_design

profile = benchmark_isp_profiles([Path("benchmark/working/cases.yaml")])[
    "isp_abilene--ospf--ebgp--kathara"
]
selection = BgpAsSelectionRequirement(
    kind="bgp_as_selection", id="exit.as65001.prefix102",
    asn=65001, prefix="198.51.102.0/24", candidates=(65002, 65003),
)
spec = build_benchmark_isp_experiment(profile, bgp_selections=(selection,))
summary = validate_routing_design(spec, Path("/tmp/abilene-policy"), runtime=True)
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

Reusable completion code lives in `src/nika/net_env/synthesis`; verifier adapters live in `src/nika/validation`; construction and validation orchestration live in `src/nika/workflows/validation`. Manual entry points and specimens live in `experiment/routing_validation`. Normal benchmark execution does not invoke the construction experiment. Saved original deployment choices and completed parameters can be loaded explicitly through `load_routing_build` and the existing startup API.

The [coverage record](../../experiment/routing_validation/coverage.json) distinguishes 289 statically checked default ISP profiles from 39 natively exercised benchmark deployment profiles. FRR and SR Linux have broader native ISP coverage. The native ISP profile regressions below extend Cisco XRd and RouterOS beyond their original two-router direct-eBGP examples. These counts describe recorded experiments before source cleanup. The retained regressions and experiment inventory reproduce their stated scopes on the current revision.

A new compatible topology supplies different inputs to this workflow. A new protocol or vendor needs supported completion bindings, configuration analysis and native observations while preserving the original requirements. Cross-domain reuse retains this construction and acceptance sequence and states the remaining modeling limits.

## ISP device profiles

`device_profile` selects a concrete network operating system, while `backend`
selects its execution environment. The existing benchmark row identity includes
both fields, so the same graph and protocol sketch can have distinct FRR, SR
Linux, IOS XR and RouterOS tasks without adding a separate `vendor` field.
IOS XR and RouterOS ISP deployments currently require explicit `igp: ospf` and
support `bgp_mode: none`, `ibgp_rr` and `ebgp`; security overlays remain excluded.

With both vendor images installed, run the retained native matrix serially:

```bash
uv run pytest tests/nika/validation/test_vendor_isp_workflow.py -m e2e -x -q
```

The matrix uses ARPANET196912 for OSPF and iBGP route reflection, and DataXchange
for multi-AS BGP. Each profile follows NetComplete, Batfish, native checks,
configuration negative controls, link failure and recovery. The coverage record
lists accepted executions separately from offline configuration compilation.

Recorded native examples include:

| Graph | BGP sketch | Profiles | Healthy predicates |
| --- | --- | --- | --- |
| ARPANET196912 | none | IOS XR, RouterOS | 20 |
| ARPANET196912 | iBGP route reflection | IOS XR, RouterOS | 36 |
| DataXchange | multi-AS BGP | IOS XR, RouterOS | 62 |
| Abilene | multi-AS BGP | RouterOS | 133 |

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

A future mixed-vendor extension should retain `device_profile` as the default
and resolve per-node overrides before allocating interfaces. Link addresses,
OSPF network type, BGP sessions and original predicates remain shared inputs;
rendering and native observations dispatch by node. All selected profiles also
need a common execution backend. Extending the existing Containerlab path is a
practical next step because SR Linux already uses it. A mixed deployment must
check interoperability on each cross-vendor link. Per-node profile overrides
are not implemented yet.

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
from nika.workflows.validation.routing import validate_routing_design

profile = deepcopy(next(
    p for p in isp_catalog_profiles().values() if p["scenario"] == "isp_dataxchange"
))
profile["deployment"].update(igp="ospf", bgp_mode="ebgp", device_profile="frr")
spec = build_benchmark_isp_experiment(profile)
summary = validate_routing_design(
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
| ISP IOS XR and RouterOS | OSPF; no BGP, iBGP RR or eBGP | RouterOS exact-prefix filters and local preference |
| Clos and campus FRR | Ethernet OSPF or eBGP, including solved export policies | Linux addressing, switching and services |
| Enterprise FRR | Tunnel-endpoint BGP and prefix/preference policies | WireGuard lifecycle and existing default-to-business VRF imports |
| SR Linux BGP fabric | IPv4 BGP and solved export policies | Existing IPv6 interface addressing |
| Direct IOS XR and RouterOS BGP | Fixed addressed peers and connected origins | RouterOS exact-prefix export filters |
| FRR OSPF/eBGP security profiles | Base interfaces, protocols, origins and ordinary policies | Existing RPKI/RTBH policy fragments and runtime attachments |

NetComplete supplies the solved values; netlab supplies protocol and policy syntax.
Prefix filters and local preference use upstream's [routing module](https://netlab.tools/module/routing/)
and [BGP policy plugin](https://netlab.tools/plugins/bgp.policy/). The latter binds
eBGP policies only. NIKA attaches the same upstream-generated admission policies
to the actual iBGP peers in the fixed ISP sketch. RouterOS lacks this generic
policy module, so NIKA retains its existing finite filter implementation.
The summary records these retained configuration responsibilities. Unsupported
protocol or policy combinations raise an error.

The pinned RouterOS BGP template needs bounded compatibility corrections for
community property names and the loopback source address. The runner applies
the names documented in [RouterOS Route Selection and Filters](https://help.mikrotik.com/docs/spaces/ROS/pages/74678285/Route%2BSelection%2Band%2BFilters)
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

## Extending verification to SDN and P4

The reusable method is to specify behavior before construction, check the candidate
with an independent analyzer, then exercise the same requirements during execution,
fault injection and recovery. Each domain needs an analyzer for its semantics.
NetComplete, netlab and Batfish currently serve the routing experiments above.
The SDN and P4 integrations below are proposed work; existing startup checks do
not establish the formal properties described here.

The current P4 fabric verifier compares P4Runtime readbacks with the generated
intent and probes endpoint traffic. Readback consistency confirms deployment of
that intent, but cannot detect a policy error shared by the generator and its
expected table entries. Author endpoint communication and isolation requirements
before generating either OpenFlow rules or P4 table entries.

Reuse the existing `ValidationIntent` reachability, isolation and waypoint shapes,
`TrafficSelector` and original predicate IDs for those requirements. Add domain
observations behind those meanings; keep P4 header and action assertions separate
from routing-protocol predicates.

| Existing scenario | Candidate to analyze | Proposed method | Initial properties |
| --- | --- | --- | --- |
| `sdn_l3_clos` | OpenFlow matches, priorities, actions, groups and physical links | NetKAT encoding checked by KATch | IPv4 delivery, isolation and permitted exits |
| `p4_dc_fabric` | `fabric.p4`, LPM entries and selector groups/members | P4-aware symbolic tests, plus a scoped forwarding model | Invalid-header and TTL handling; endpoint delivery and allowed ECMP exits |
| `p4_dc_gateway` | `gateway.p4` and its concrete control-plane entries | Begin with the stateless IPv4 forwarding subset | Forwarding and access-control properties under explicit state assumptions |

### SDN forwarding properties

[Header Space Analysis](https://www.usenix.org/conference/nsdi12/technical-sessions/presentation/kazemian)
models devices as packet-header transformations and composes them through the
network. [NetKAT](https://doi.org/10.1145/2535838.2535862) gives packet processing
an algebraic semantics. [KATch](https://github.com/cornell-netlab/KATch) provides an
external equivalence and containment checker for NetKAT policies. These methods
give the proposed SDN adapter an established semantic basis.

For `sdn_l3_clos`, encode the actual forwarding rules, including priority masking,
prefix matches, rewrites and output ports, and compose them with the physical
links. Build the requirements separately from endpoint ownership and authored
communication policy. For example, with input class `I`, candidate forwarding
relation `F`, and forbidden egress class `U`, isolation requires:

```text
I ; F ; U = 0
```

KATch can check this as a policy equivalence. A separate delivery requirement is
necessary: an empty forwarding relation also satisfies isolation. Model SELECT
groups as nondeterministic choices when checking safety over all permitted exits.
Reachability in that abstraction establishes a possible path; it alone does not
establish delivery for every hash choice, balanced traffic, or absence of loops.
Requirements for guaranteed delivery must also exclude drops and nonterminating
paths under the stated model.

The first adapter should cover the IPv4 rules emitted by the existing
`build_forwarding_rules` function. ARP responder actions, controller transitions
and unsupported OpenFlow features need explicit exclusions or separate checks.
Checking a fixed rule set establishes properties of that state; native fault and
recovery tests remain necessary for controller behavior.

### P4 program and table behavior

A P4 configuration includes both the program and its control-plane table entries.
Checking only `build_forwarding_intent` output leaves the program's interpretation
of those entries unchecked. Successful compilation establishes compiler acceptance,
but provides no independent forwarding-policy proof.

[P4Testgen](https://p4lang.github.io/p4c/p4testgen.html), part of upstream p4c,
symbolically generates input packets, control-plane entries and expected output
packets. Its BMv2/v1model target matches the architecture of NIKA's current fabric
program. It can exercise invalid headers, TTL boundaries, LPM actions and packet
rewrites, and generate counterexamples to supplied assertions. A bounded successful
test run is testing evidence. Statement coverage is not a proof of the original
network specification.

Before using these tests for scenario acceptance, establish how the tool constrains
the concrete scenario entries and compare outputs with independently authored
requirements. Tests using the tool's own synthesized entries cannot substitute for
checking NIKA's candidate entries. A scoped network model can complement program
tests, provided its assumed P4 action semantics and selector abstraction remain
explicit.

[p4v](https://www.cs.cornell.edu/~jnfoster/papers/p4v.pdf) provides the related
proof approach: verify program safety properties under stated control-plane
assumptions. Its paper supplies a methodological reference; NIKA has not integrated
or established compatibility with that verifier. Begin implementation with
`p4_dc_fabric` before the gateway, whose registers, queues, telemetry and changing
load-balancer state require more than P4Testgen's single-packet model. Keep those
behaviors as native obligations until an appropriate stateful model is available.
