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
3. **Render configurations.** Existing NIKA constructors bind typed parameters to the original graph, protocols and services. This deterministic rendering supplies a candidate; it does not itself prove the candidate correct.
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

`experiment/routing_validation/benchmark_isp.py --catalog --run` checks every registered default ISP profile statically; `test_benchmark_routing.py` exercises the ISP deployment profiles in the benchmark inventory natively. FRR and SR Linux have broader ISP coverage; Cisco XRd and RouterOS cover two-router direct eBGP.

A new compatible topology supplies different inputs to this workflow. A new protocol or vendor needs supported completion bindings, configuration analysis and native observations while preserving the original requirements. Cross-domain reuse retains this construction and acceptance sequence and states the remaining modeling limits.
