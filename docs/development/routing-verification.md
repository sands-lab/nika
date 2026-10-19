# Specification-driven routing construction

NIKA prepares routing environments from explicit behavior and a partial network design. A topology graph supplies structure; the experiment specifies protocols, route-reflector roles, prefix ownership and intended routing behavior. A converged configuration can implement the wrong policy, so acceptance requires both independent configuration analysis and native execution against the original requirements.

## Construction and acceptance

```text
Public graph + authored behavior + fixed routing sketch
                         |
             NetComplete parameter completion
                         |
          Existing NIKA configuration generators
                         |
        Batfish checks of original behavioral predicates
                         |
       Native checks of the same predicates and runtime properties
                         |
          Declared fault effects and complete recovery
```

1. **Specify behavior.** Typed requirements name permitted paths, exact ECMP sets, BGP candidate order, adjacencies and communication or isolation. Requirements and incident expectations precede synthesis. Fixed addressing and session bindings resolve them to concrete `ValidationIntent` predicates.
2. **Complete supported parameters.** [NetComplete's partial-synthesis formulation](https://netcomplete.ethz.ch/files/netcomplete-nsdi2018.pdf) supplies the method: retain a routing sketch and solve its unknown configuration values. NIKA delegates supported path costs, export decisions and import preferences to the pinned upstream implementation. Solver output cannot redefine the requirements.
3. **Generate configurations.** Bind the completed parameters to the original graph, protocols and services. The optional netlab ISP path delegates topology transformation and vendor syntax to upstream netlab. Existing NIKA constructors cover the other supported designs. Generation supplies a candidate; independent checks establish acceptance.
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

The supported construction scope is Kathara FRR and IOS XR ISP plans using IPv4
OSPF, with no BGP, iBGP RR, or ordinary eBGP. Prefix lists and per-prefix
local-preference use netlab's [routing module](https://netlab.tools/module/routing/)
and [BGP policy plugin](https://netlab.tools/plugins/bgp.policy/). OSPF costs must
fit upstream's 1–65,534 range. FRR IPv4 Level-2 IS-IS with optional ordinary iBGP
RR uses the same adapter and workflow: NetComplete solves the declared additive
shortest-path requirements, and netlab generates IS-IS NETs, interfaces and wide metrics from
the fixed inputs. NIKA supplies the existing NET identities and passive-loopback
setting through upstream topology inputs. The concrete IS-IS/BGP model supports
connected business prefixes and same-AS RR sessions. IS-IS with eBGP remains
outside that model.

Security overlays and registered configuration-failure bindings are not covered;
the experiment retains its declared negative controls and link incidents. RouterOS7 lacks
upstream generic routing policies; SR Linux still needs an adapter for deploying
netlab's native CLI through NIKA's document-based backend. These profiles retain
the existing implementation and are rejected when netlab generation is requested.
No automatic fallback substitutes a different generator.

The construction saves `netlab/topology.yml`, the transformed topology, upstream
configuration fragments and the final `candidate-configs`. Batfish checks those
final configurations. For FRR, snapshot import normalizes the interface-level
`ip ospf passive` command to `passive-interface` for the pinned parser, using
[FRR's documented semantics](https://docs.frrouting.org/en/stable-10.2/ospfd.html).
This normalization is limited to one default-table OSPF process without mixed
passive syntaxes. For IS-IS, the existing concrete FRR-to-IOS-XR projection reads
the generated interface blocks, including separately emitted address and protocol
blocks. It retains point-to-point network type, metrics, passivity and NET identity.
The projection accepts the `level-2` CLI abbreviation and excludes the emitted
logging, dynamic-hostname and LSP-timer commands from its steady-state model;
[FRR's IS-IS documentation](https://docs.frrouting.org/en/stable-10.2/isisd.html)
defines the routing commands, and its [timer implementation](https://github.com/FRRouting/frr/blob/stable/10.5/isisd/isis_cli.c)
defines the combined LSP-timer syntax.
For IS-IS+iBGP RR, the parser separates interleaved concrete protocol blocks and
retains origins, ASNs, peer addresses, source interfaces and RR roles.
The model covers prefix-only policy and connected origins. Netlab's display,
community-export and AS-path multipath defaults do not affect route selection
within this subset: the sessions remain inside one AS and carry empty AS paths.
Configurations combining those AS-path defaults with external peers are rejected.
Both paths read candidate commands, including mutations. Native validation deploys
the generated configuration through the ordinary NIKA
startup API and evaluates the original predicates, negative controls, link loss
and recovery. Negative-control command bindings follow the emitted syntax while
their expected violation sets remain fixed. The successful build loader reuses
the saved configurations, rather than regenerating a candidate at startup.

This delegates configuration transformation and syntax to an established tool;
it does not add a compiler-correctness claim. Acceptance continues to come from
independent checks of the original requirements. The default benchmark path and
published case definitions do not change.
