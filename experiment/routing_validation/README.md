# Routing construction and verification experiments

Construct a routing configuration with NetComplete, then check it against requirements authored before synthesis. The same NIKA workflow supports ISP, Clos, campus and enterprise sketches within the model scopes below. Run this workflow when preparing or changing an environment. Normal benchmark execution keeps its deployment defaults and does not invoke this experiment.

## Run an experiment

Install NIKA with `uv sync --extra batfish` and the normal Docker, Kathara and, for SR Linux, Containerlab prerequisites. Build the pinned analysis tools:

```bash
git clone https://github.com/nsg-ethz/synet-plus.git /tmp/nika-netcomplete
git -C /tmp/nika-netcomplete checkout 6b7816fdeb1ffa3f41c3954b2102651b120cf4d5
docker build -f scripts/netcomplete/Dockerfile -t nika/netcomplete:experiment /tmp/nika-netcomplete
docker build -t nika/batfish:2025.07.07.2423-frr-next-hop-self scripts/batfish
```

Run one specification through synthesis, static checks, native execution, declared faults and recovery:

```bash
uv run python experiment/routing_validation/experiment.py experiment/routing_validation/clos.json --output /tmp/routing-clos --runtime
```

Use `campus.json`, `enterprise.json`, `clos-m.json` or `min3clos.json` for the other specimens. Run heavy environments sequentially. Omit `--runtime` for static analysis only. A successful `summary.json` records `status: passed` and either `validation_scope: static-only` or `static-and-runtime`. Unsupported or failed required checks reject the construction.

The output directory contains the original and resolved specifications, NetComplete inputs and solution, completed parameters, candidate configurations, verifier reports and final summary. These are run artifacts; they are not committed source files. A failed rerun removes the previous acceptance summary. Snapshot exporters replace their generated snapshots so obsolete devices cannot enter later analyses.

The workflow closes its own native session and removes a Batfish service only when it started that service. It preserves an existing analyzer. For independently managed analyzers, pass `batfish_verifier=BatfishVerifier(host=..., port=...)` to the Python workflow and manage that service in the caller.

## Cover benchmark ISP deployments

Prepare specifications from the benchmark's selected topology, IGP, BGP mode, backend and device profile:

```bash
uv run python experiment/routing_validation/benchmark_isp.py benchmark/working/cases.yaml benchmark/releases/0.2.0/dev.yaml benchmark/releases/0.2.0/test.yaml --output /tmp/benchmark-isp
```

Add `--run` for NetComplete and Batfish, and `--runtime` for execution and fault recovery. `--scenario isp_abilene` restricts the selected inventory. To select registered default ISP deployments instead of benchmark variants:

```bash
uv run python experiment/routing_validation/benchmark_isp.py --catalog --output /tmp/catalog-isp --run
```

`coverage.json` and `results.json` in the output directory distinguish unsupported, prepared and passed profiles. The runner saves progress after each completed profile. Published releases and the working benchmark manifests retain their existing deployment choices.

## Generate ISP configurations with netlab

For FRR/IOS XR OSPF or FRR Level-2 IS-IS ISP profiles, install
`uv sync --extra batfish --extra netlab` and add `--config-generator netlab` to either experiment runner.
The Python workflow accepts the same `config_generator="netlab"` option.
NetComplete chooses costs and import preferences; upstream netlab transforms the
fixed topology and generates interfaces, IGP, BGP/RR and routing policies. The
existing Batfish and native checks still evaluate the original requirements.

The scope includes ordinary OSPF-only, OSPF with iBGP RR/eBGP, and FRR IS-IS-only
ISP designs. IS-IS with BGP, RouterOS,
SR Linux, security overlays, registered configuration-failure bindings and other
scenario families are currently rejected
when this option is selected. The [implementation guide](../../docs/development/routing-verification.md#delegate-configuration-generation-to-netlab)
explains the upstream limitations and saved artifacts. Omitting the option keeps
the existing construction path. This is an experiment option, not a NIKA CLI
command or an automatic change to benchmark cases.

## Reuse a saved construction

Load the original deployment choices and completed parameters into the normal startup API:

```python
from pathlib import Path
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from nika.workflows.validation.routing_build import load_routing_build

session_id = start_net_env(**load_routing_build(Path("/tmp/routing-clos")))
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
| FRR IS-IS | Restricted translation of concrete IPv4 Level-2 configuration to IOS XR | Original IS-IS and supported iBGP predicates; FRR IS-IS/eBGP is unsupported |
| Nokia SR Linux | Restricted concrete-configuration projection | Original IGP/BGP predicates on SR Linux |
| Cisco IOS XR | Native parser | Direct two-router eBGP on XRd |
| MikroTik RouterOS | Restricted IPv4/direct-eBGP projection | Original predicates on RouterOS |
| Enterprise | Explicit static-underlay projection | WireGuard, VRF routing and isolation |
| Clos ECMP | Routing model | Installed ECMP sets, including FRR multipath limits |

The pinned Batfish image includes an [FRR next-hop correction](../../scripts/batfish/frr-next-hop-self.patch). Projections reject unsupported statements instead of silently assigning them supported semantics. Static/native agreement covers the declared experiments, rather than universal implementation equivalence.

## Reproduce regression checks

The [coverage record](coverage.json) summarizes earlier executions and their limits. Current regressions exercise the shared implementation:

```bash
uv run pytest tests/nika/validation -m 'unit or contract' -q
uv run pytest 'tests/nika/validation/test_routing_completion.py::test_completion_configuration_execution_and_registered_failures[clos]' -q
uv run pytest 'tests/nika/validation/test_benchmark_routing.py::test_as_import_and_igp_completion_preserve_benchmark_failures[isp_abilene-kathara]' -q
uv run pytest tests/nika/validation/test_vendor_routing.py -m e2e -q
```

Vendor execution requires locally installed licensed images. Missing vendor images cause the corresponding native test to skip; a skipped test is not validation evidence. See the [method and concrete Abilene example](../../docs/development/routing-verification.md) for the correctness argument and input/output examples.
