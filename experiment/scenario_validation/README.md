# Scenario construction and verification experiments

Check generated network configurations against requirements authored before construction. Routing experiments use NetComplete and Batfish; SDN and P4 forwarding experiments use NetKAT through KATch. The shared workflow covers ISP, Clos, campus, enterprise and the supported packet fabrics within the model scopes below. Run this workflow when preparing or changing an environment. Normal benchmark execution keeps its deployment defaults and does not invoke this experiment.

## Coverage boundaries

The shared workflow currently covers the supported ISP profiles, `dc_clos`,
`campus_lan`, `enterprise_branch`, `min3clos`, `iosxr_simple_bgp`,
`routeros_simple_bgp`, `sdn_l3_clos`, `p4_dc_fabric` and `p4_dc_gateway`.
Use the retained test matrices below to reproduce acceptance for a deployment.
Adapter support alone does not imply native acceptance for every size, vendor or fault.

`k8s_lab` and `llmd_lab` retain their existing runtime checks and are not integrated
with this specification-driven experiment. For SDN and P4, recorded end-to-end
acceptance covers sizes `s` and `m` and the declared ICMP forwarding requirements. It does
not establish full controller, P4 program or stateful service correctness.

## Run an experiment

Prerequisites:

- A Linux host with NIKA installed as described in the [installation guide](../../docs/operations/installation.md), including Docker and Kathara.
- Containerlab, only for SR Linux specimens and ISP profiles with `--backend containerlab`.
- For the IOS XR and RouterOS examples, the vendor router images from [Prepare vendor images](../../docs/operations/installation.md#prepare-vendor-images). RouterOS needs `/dev/kvm`.

From the repository root, install the analysis extras and build the pinned analysis tools:

```bash
uv sync --extra batfish --extra netlab
git clone https://github.com/nsg-ethz/synet-plus.git /tmp/nika-netcomplete
git -C /tmp/nika-netcomplete checkout 6b7816fdeb1ffa3f41c3954b2102651b120cf4d5
docker build -f scripts/netcomplete/Dockerfile -t nika/netcomplete:experiment /tmp/nika-netcomplete
docker build -t nika/batfish:2025.07.07.2423-frr-next-hop-self scripts/batfish
```

Run one specification through synthesis, static checks, native execution, declared faults and recovery:

```bash
uv run python experiment/scenario_validation/experiment.py experiment/scenario_validation/clos.json --output /tmp/routing-clos --runtime
```

Use `campus.json`, `enterprise.json`, `clos-m.json` or `min3clos.json` for the other specimens. The two-router vendor scenarios derive their specification from the scenario's own router declarations instead:

```bash
uv run python experiment/scenario_validation/experiment.py --direct-bgp iosxr_simple_bgp --routing-model iosxr-bgp --output /tmp/routing-xrd --runtime
uv run python experiment/scenario_validation/experiment.py --direct-bgp routeros_simple_bgp --routing-model routeros-bgp --output /tmp/routing-routeros --runtime
```

Run heavy environments sequentially. Omit `--runtime` for static analysis only. A successful `summary.json` records `status: passed` and either `validation_scope: static-only` or `static-and-runtime`. Unsupported or failed required checks reject the construction.

The output directory contains the original and resolved specifications, NetComplete inputs and solution, completed parameters, candidate configurations, verifier reports and final summary. These are run artifacts; they are not committed source files. A failed rerun removes the previous acceptance summary. Snapshot exporters replace their generated snapshots so obsolete devices cannot enter later analyses.

The workflow closes its own native session and removes a Batfish service only when it created that service, and stops a stopped analyzer again after restarting it. It leaves a running analyzer in place. For independently managed analyzers, pass `batfish_verifier=BatfishVerifier(host=..., port=...)` to the Python workflow and manage that service in the caller.

## Cover benchmark ISP deployments

Prepare specifications from the benchmark's selected topology, IGP, BGP mode, backend and device profile:

```bash
uv run python experiment/scenario_validation/benchmark_isp.py benchmark/working/cases.yaml benchmark/releases/0.2.0/dev.yaml benchmark/releases/0.2.0/test.yaml --output /tmp/benchmark-isp
```

Add `--run` for NetComplete and Batfish, and `--runtime` for execution and fault recovery. `--scenario isp_abilene` restricts the selected inventory. To select registered default ISP deployments instead of benchmark variants:

```bash
uv run python experiment/scenario_validation/benchmark_isp.py --catalog --output /tmp/catalog-isp --run
```

`coverage.json` and `results.json` in the output directory distinguish unsupported, prepared and passed profiles. The runner saves progress after each completed profile. Published releases and the working benchmark manifests retain their existing deployment choices.

## Generate configurations with netlab

Install `uv sync --extra batfish --extra netlab` and add `--config-generator netlab`
to either experiment runner. The Python workflow accepts
`config_generator="netlab"`. NetComplete solves the declared routing choices;
netlab generates protocol and policy configuration; Batfish and native execution
check the original requirements.

This path supports the existing ISP, Clos, campus, enterprise and direct-BGP
routing sketches. ISP devices include FRR, IOS XR, SR Linux and RouterOS within
their supported protocol scopes. NIKA retains the configuration features missing
from upstream templates, such as RouterOS filters and enterprise VRF imports;
the result summary lists these responsibilities. See the
[configuration generation scope](../../docs/development/scenario-verification.md#4-generate-the-target-device-configuration)
for protocol limits and model assumptions.

Omitting the option keeps the existing construction path. Normal benchmark
startup can reuse saved candidates; it does not run NetComplete, netlab or
Batfish. This option adds no NIKA CLI command and changes no published tasks.

## Reuse a saved construction

Load the original deployment choices and completed parameters into the normal startup API:

```python
from pathlib import Path
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from nika.workflows.validation.scenario_build import load_scenario_build

session_id = start_net_env(**load_scenario_build(Path("/tmp/routing-clos")))
try:
    # Run the intended workload or troubleshooting task in this session.
    ...
finally:
    close_session(session_id=session_id)
```

This path uses the same deployment arguments as native verification. It runs normal readiness checks. Netlab constructions reuse their saved candidate configurations; other constructions reuse the existing deterministic configuration generators. It does not call NetComplete or Batfish, or repeat the validation fault sequence. The loader requires a successful summary and the saved parameters when completion was declared. Check the saved `validation_scope` when selecting a construction; loading a static-only result adds no runtime evidence. Batch benchmark selection of saved constructions is not connected automatically.

## Tool responsibilities and model scope

| Completion | Original behavior | Solver output | Fixed design |
| --- | --- | --- | --- |
| `bgp_fabric` | Device adjacencies, prefix ownership and shortest-AS-path propagation | Per-neighbor, exact-prefix export decisions | Graph, ASNs, origins and uniform import attributes |
| `ospf_area` | Declared intra-area paths, including exact ECMP sets | Symmetric positive interface costs | Area boundaries, ABRs, addressing and services |
| `isis_level` | Declared IPv4 Level-2 paths | Symmetric metrics through the shared shortest-path solver | NETs, level, prefix attachments and addressing |
| `bgp_imports` | Per-device, per-prefix candidate order | Import permit/deny and local preference | Neighbor identities and remote advertisement assumptions |
| `bgp_as_imports` | Ordered neighboring ASes for an original prefix | Consistent import policies at the AS's physical ingresses | AS membership, sessions, RR roles and security policies |

IGP completion shares metric variables between healthy and declared fault-state graphs. NetComplete must satisfy the original single-path or ECMP requirements in all those states with one parameter assignment. Parallel interfaces share a symmetric router-pair metric; unequal parallel reference metrics are unsupported. AS import completion composes with IGP completion over the fixed sketch; it does not implement NetComplete's full BGP-to-OSPF feedback.

Batfish checks original predicates against the final candidate configuration. Native checks use the same predicates, with separate original requirements for behavior the static model cannot establish. For modeled configuration faults, Batfish predicts the violated original predicates before injection; execution must reproduce that set and satisfy the independently declared fault-state requirements. Recovery must restore the complete healthy requirements.

| Platform or domain | Static analysis | Native obligation |
| --- | --- | --- |
| FRR OSPF/BGP | Native configuration parser | Sessions, routes, flows and declared fault/recovery behavior |
| FRR IS-IS | Restricted translation of concrete IPv4 Level-2 configuration to IOS XR | Original IS-IS, ordinary iBGP RR and direct eBGP predicates |
| Nokia SR Linux | Restricted concrete-configuration projection | Original IGP/BGP predicates on SR Linux |
| Cisco IOS XR | Native parser | Original ISP OSPF/BGP and direct eBGP predicates on XRd |
| MikroTik RouterOS | Restricted concrete IPv4 OSPF/BGP projection | Original predicates on RouterOS |
| Enterprise | Explicit static-underlay projection | WireGuard, VRF routing and isolation |
| Clos ECMP | Routing model | Installed ECMP sets, including FRR multipath limits |

The pinned Batfish image includes an [FRR next-hop correction](../../scripts/batfish/frr-next-hop-self.patch). Projections reject unsupported statements instead of silently assigning them supported semantics. Static/native agreement covers the declared experiments, rather than universal implementation equivalence.

## Verify SDN and P4 fabrics

Use the same experiment API for the current OpenFlow and P4Runtime fabrics. Docker
must have the normal NIKA scenario images and the pinned KATch artifact:

```bash
docker pull julesjacobs/katch@sha256:7184375abfbcf38e6c1e176637507272006d4e4bfa8ce30dab60069dcd6da410
```

```python
from pathlib import Path
from nika.workflows.validation.forwarding import build_forwarding_experiment
from nika.workflows.validation.scenario import validate_scenario_design

spec = build_forwarding_experiment("sdn_l3_clos", topo_size="s")
result = validate_scenario_design(spec, Path("/tmp/sdn-validation"), runtime=True)
assert result["status"] == "passed"
```

Select `p4_dc_fabric` or `p4_dc_gateway` through the same builder. Topology sizes
`s`, `m` and `l` use the same procedure; a supported size is not a completed native
run. Omit `runtime=True` for static analysis only. The builder declares client-service
ICMP communication and unowned-address isolation before generating configuration.
For another policy, author the original `ScenarioExperiment` packet requirements
with `routing_model="netkat"`. Saved specifications also work with `experiment.py`.

KATch checks the restricted forwarding model. Native checks evaluate the same
original predicate IDs, reproduce a declared configuration counterexample and
restore the healthy specification after endpoint-interface shutdown. NetComplete
and netlab are not needed for these fixed match/action candidates. Benchmark
startup does not invoke KATch, and the registered benchmark failures keep their
existing behavior. See the [method and P4 scope](../../docs/development/scenario-verification.md#sdn-and-p4-forwarding-verification)
before interpreting acceptance as evidence about a whole P4 program or controller.

NIKA's batch driver loads the network once and evaluates each original assertion
through the pinned KATch parser and equivalence procedure in one JVM. It records
normal assertion failures and continues through the remaining queries. Parser
errors, resource failures, missing results and timeouts reject acceptance. The
specification's `solver_timeout_sec` budget covers the batch; the existing memory
and CPU limits still apply. The installed KATch image supplies the Java compiler
for this driver, so the host needs no additional Java installation.

## Reproduce regression checks

Compare the full scenario, device profile, IGP and BGP mode when assessing
coverage. Offline generation and model import provide no native acceptance
evidence. Each experiment saves its own specifications, reports and summary
under the selected output directory; retain those artifacts with experiment results.

The routing, native-vendor and packet-fabric tests share
[`assert_scenario_acceptance`](../../tests/support/scenario_validation.py) for
predicate coverage, operating states, counterexamples, recovery and session closure.
Extend their parameter matrices to cover another compatible deployment. Keep
protocol-specific requirements in the specification and retain any additional
protocol assertions. Routing tests share a pinned Batfish instance on an allocated
local port, so they preserve independently running analyzers.

Run the retained regressions against the shared implementation:

```bash
uv run pytest tests/nika/validation -m 'unit or contract' -q
uv run pytest 'tests/nika/validation/test_routing_completion.py::test_completion_configuration_execution_and_registered_failures[clos-netlab]' -q
uv run pytest 'tests/nika/validation/test_benchmark_routing.py::test_as_import_and_igp_completion_preserve_benchmark_failures[isp_abilene-kathara]' -q
uv run pytest tests/nika/validation/test_vendor_routing.py -m e2e -q
uv run pytest tests/nika/validation/test_netkat.py -m e2e -q
```

Vendor execution requires locally installed licensed images. Missing vendor images cause the corresponding native test to skip; a skipped test is not validation evidence. See the [method and concrete Abilene example](../../docs/development/scenario-verification.md) for the correctness argument and input/output examples.
