# Create benchmark tasks

Add a network environment, an injectable failure, or a benchmark task to NIKA. Each part ends with commands that show the change working in a running session. Terms follow the [glossary](../../GLOSSARY.md).

Core contracts: [`NetworkEnvBase`](../../src/nika/net_env/base.py), [`ProblemBase`](../../src/nika/problems/base.py), and the [`benchmark` workflows](../../src/nika/workflows/benchmark/).

## How a task is built

A benchmark task combines three things:

- a **network environment** from `src/nika/net_env/`
- a **network incident**: one or more registered failures with their injection parameters, or a healthy baseline
- a **network traffic pattern**: optional traffic that runs before or during troubleshooting

Each run writes its agent output and evaluation under `results/{session_id}/`.

Static tasks use top-level `problem` or `problems` with `inject`. `problem: healthy` selects a healthy baseline. Scheduled tasks declare failures inside `schedule.events` and omit the top-level fields. The two forms cannot be combined. See [Incident scheduling](../benchmarks/scheduling.md) for complete task files and timing rules.

To run the steps of a session by hand:

```shell
nika env run <scenario> [-s s|m|l]
nika failure inject <problem> [--set key=value ...]
nika failure inject <problem-a> <problem-b> [--set problem-a.field=value ...]
nika traffic run <type> ...
nika agent run -a <agent> ...
nika session close -y
nika eval metrics
```

`nika benchmark run` performs the same steps for each task in a benchmark: it deploys the environment, injects the incident, runs the agent, closes the session, and evaluates the result.

## Write a network environment

Read the [network scenario reference](../operations/network-scenarios.md) first. An existing parameterized scenario may already cover your topology.

1. Put a single-backend lab under `src/nika/net_env/<scenario>/`. Only multi-backend scenarios use backend subdirectories, such as `src/nika/net_env/isp/kathara/` and `src/nika/net_env/isp/containerlab/`. Put helpers shared by backends in `src/nika/net_env/utils/kathara/` or `src/nika/net_env/utils/containerlab/`.
2. Write a class that extends `NetworkEnvBase`. It sets `LAB_NAME`, builds the backend lab, and sets `self.name` and `self.desc`. Declare each machine's role with `declare_machine()` before `load_machines()` builds the host and router lists.
3. If the scenario has sizes, accept `topo_size` in `__init__` and list the sizes in `NetEnvSpec.topo_size`.
4. Register the scenario in [`net_env_pool.py`](../../src/nika/net_env/net_env_pool.py) with import-safe metadata and a lazy module and class binding.
5. Pin every image. Use `nika_image("<name>")` for local `nika/*` builds, which carry the benchmark version. Use a `repo:tag@sha256:<digest>` reference for upstream images. Never use a floating tag such as `latest` on its own.

A minimal scenario class looks like this:

```python
from Kathara.model.Lab import Lab
from Kathara.manager.Kathara import Kathara

from nika.net_env.base import NetworkEnvBase
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.spec import NodeRole


class MyScenario(NetworkEnvBase):
    LAB_NAME = "my_scenario"

    def __init__(self, topo_size: str = "s"):
        super().__init__()
        self.name = self.LAB_NAME
        self.desc = "Short operator-facing description."
        self.instance = Kathara.get_instance()
        self.lab = Lab(self.name)

        pc1 = self.lab.new_machine("pc1", image=nika_image("base"))
        pc2 = self.lab.new_machine("pc2", image=nika_image("base"))
        self.lab.connect_machine_to_link(pc1.name, "A")
        self.lab.connect_machine_to_link(pc2.name, "A")

        self.declare_machine(pc1.name, role=NodeRole.HOST)
        self.declare_machine(pc2.name, role=NodeRole.HOST)
        self.load_machines()
```

Register its metadata in the pool, without importing the backend package:

```python
"my_scenario": NetEnvSpec(
    lab_name="my_scenario",
    module="nika.net_env.my_scenario.lab",
    class_name="MyScenario",
    tags=("link", "icmp", "pc"),
    supported_backends=("kathara",),
    topo_size=["s", "m", "l"],
),
```

For a scenario with more than one backend, set `backend_bindings` to one `BackendEnvBinding` per backend. Keep the scenario ID and its backend-neutral meaning the same across bindings.

Check the environment:

```shell
uv run nika env list
uv run nika env run my_scenario -s s
uv run nika session inspect
uv run nika session close -y
```

---

## Reference: scenario metadata and target roles

`NetEnvSpec` is the only place for scenario metadata. Do not repeat `TAGS` or `TOPO_SIZE` on the class, and do not add the scenario name to framework lists. These optional fields change how the scenario is used:

- `family`: release split-coverage family (defaults to the scenario ID).
- `heavy_lab`: host-shared heavy lab that the benchmark runner schedules exclusively.
- `benchmark_excluded`: keeps the scenario out of the generated candidate pool.
- `coverage_major`: lists the scenario as major in pool audits and coverage reports.
- `licensed_images`: the images need a licensed vendor download (`install.sh --with-vendor-images`).
- `k8s_image_cache`: caches workload images on the host and loads them into k3s.

Scenario-specific target knowledge stays on the lab class:

- `target_roles()` maps each role to its nodes: `hosts`, `routers`, `web`, `attacker_pool`, `access_routers`, `bgp_originators`, and the optional `edges` and `l2_endpoints`. Override it when the generic inventory is too broad, for example to exclude guest hosts or spine routers.
- `default_probe_path()` returns the scenario's default `ProbePath` (probe source host, destination, and URLs), which injection helpers and symptom tests use.

Each SNDlib graph is its own scenario ID (`isp_abilene`, `isp_france`, and so on). Base ISP scenarios omit `-s` and still accept protocol flags. Kathara (FRR) and Containerlab (Nokia SR Linux) share the same IDs. RPKI and RTBH are named variants only:

```shell
uv run nika env run isp_abilene --igp isis
uv run nika env run isp_abilene --bgp-mode ibgp_rr
uv run nika env run isp_abilene --bgp-mode ebgp
uv run nika env run isp_france --backend containerlab --device-profile nokia_srlinux
uv run nika env run isp_abilene_ebgp_rpki
uv run nika env run isp_abilene_ebgp_rtbh
uv run nika traffic run matrix-replay --mode demands --max-intervals 1 --unit K --background
```

See the [SNDlib ISP scenarios reference](../operations/network-scenarios.md#sndlib-isp-scenarios) for the shared compiler, backend bindings, and control options.

## Write an injectable failure

Put each failure under `src/nika/problems/<failure_domain>/`. The directory name must match the class's `failure_domain`. Base classes and helpers shared across domains go in `src/nika/problems/support/`, which must not define registered failures. Attacks, spoofing, and poisoning failures go under `security`.

A failure is one `ProblemBase` subclass. It implements injection, verification, recovery, and ground truth through `get_ground_truth()`. Do not split one failure into separate Detection, Localization, and RCA classes.

```python
from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca.inventory import interface_on


class MyFaultParams(BaseModel):
    host_name: str = Field(description="Target host.")
    intf_name: str = Field(default="eth0", description="Target interface.")


class MyFault(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name = "my_fault"
    description = "Link attachment is down on the target interface."
    TAGS = ["link"]
    Params = MyFaultParams

    symptom_desc = "Users report intermittent connectivity."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def inject_fault(self, params: MyFaultParams):
        self._original_state = self.runtime.get_interface_operstate(params.host_name, params.intf_name)
        if self._original_state != "up":
            raise RuntimeError("The target interface must be up before injection.")
        self.runtime.set_interface_state(params.host_name, params.intf_name, "down")

    def root_cause_resources(self, params: MyFaultParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def verify_fault(self, params: MyFaultParams) -> dict:
        operstate = self.runtime.get_interface_operstate(params.host_name, params.intf_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=operstate == "down",
            details={"host": params.host_name, "intf": params.intf_name, "operstate": operstate},
        )

    def recover_fault(self, params: MyFaultParams) -> dict:
        original = getattr(self, "_original_state", None)
        if original not in {"up", "down"}:
            return build_verify_result(self.root_cause_name, False, {"error": "Original interface state is unavailable."})
        self.runtime.set_interface_state(params.host_name, params.intf_name, original)
        observed = self.runtime.get_interface_operstate(params.host_name, params.intf_name)
        return build_verify_result(self.root_cause_name, observed == original, {"operstate": observed})
```

Key rules:

- Set `failure_domain`, `root_cause_name`, and `Params` on the class. NIKA builds `META` from them, so do not write it by hand.
- `inject_fault()` changes only the selected lab instance.
- Leave no change that singles out the target beyond the root cause. A supporting setting that the fault needs but that is not its root cause goes on every comparable device. For example, a policy that admits a prefix flood goes on all routers, as `bgp_max_prefix_exceeded` does. Agents can compare devices.
- `verify_fault()` proves that the injection artifacts are present (nft, tc, config, process, or quota). A failed verification marks the injection as failed and stops the run. Put slow network-impact probes in the symptom API, `nika.validation.symptom.evaluate_symptom`, not here. Its contracts live under [`src/nika/validation/symptom/`](../../src/nika/validation/symptom/).
- `recover_fault(params)` undoes only this failure's changes, on the instance that injected them, in the same Python process. Save the original state in memory before you change anything. Return `verified=True` only after you read back the restored state, and report a missing snapshot or a failed restoration explicitly. Test repeated recovery, partial injection, and healthy behavior after protocols converge.
- `root_cause_resources(params)` gives NIKA the structured root-cause ground truth from the injection parameters. Do not keep a second root-cause table. Use `link_containing_endpoint` for controller-side cable faults (`link_down`, `link_flap`, `link_packet_corruption`), and `interface_on` when the mutated object is an interface. See [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).
- `oracle_playbook(params)` returns the reference diagnosis an operator would follow to confirm the fault through the agent's MCP tools. A contract test rejects a failure without one. See [Write a playbook for a new failure](../benchmarks/oracle-solver.md#write-a-playbook-for-a-new-failure).
- `Params` must be a Pydantic model. `nika failure describe` and benchmark YAML validation use it as the injection schema.
- Declare where the failure applies with `TAGS` (a subset of the scenario tags), optional `COMPATIBLE_COLUMNS` (shared sets live in [`problems/support/compatible_columns.py`](../../src/nika/problems/support/compatible_columns.py)), `supported_backends`, and `isp_protocol` when the failure needs a specific ISP protocol stack. `ProblemBase.is_compatible()` is the one predicate that the registry and benchmark generation use.

Check the failure:

```shell
uv run nika failure list
uv run nika failure describe my_fault
uv run nika env run my_scenario -s s
uv run nika failure inject my_fault --set host_name=pc1 --set intf_name=eth0
uv run nika failure ps
uv run nika agent run --agent oracle
```

The last command runs your `oracle_playbook` against the injected failure and writes `oracle.json` to the session directory.

---

## Reference: failure text, device compatibility, and shortcuts

**Text.** `description` says what the failure ID means for the agent submission ontology, and it is the preferred registry text. Name only the root cause. Leave out the injection method, artifact names, verification evidence, and comparisons such as "X fails while Y still works". `symptom_desc` is optional user-facing symptom text. When `description` is omitted, the registry falls back to `symptom_desc`, then to `root_cause_name`.

**Shortcuts NIKA already removes.** When an environment starts, NIKA deletes `/hostlab` inside every Kathara node, which holds that node's original lab files. It also saves every FRR router's running configuration. After injection, it recreates each FRR router's saved configuration file with that router's own content and gives all of them one shared timestamp.

**Multi-failure recovery.** `MultiFaultProblem.recover_fault()` undoes the attempted injections in reverse order. It stops at a failed recovery and keeps that step and the earlier ones for retry, which preserves the order of dependent snapshots.

**Device platforms.** Set `native_router_operations` when the failure can target routers with a fault-operations provider: IOS XR, RouterOS, and SONiC. List the operations in `RouterFaultOps` (`problems/support/router_ops.py`) that its native path calls through `native_fault_ops()` or `router_fault_ops()`. Use an empty set when the failure acts outside the router OS, such as on the container interface. A failure whose FRR implementation needs nothing else from the node can list `frr_shell`, which platforms that route with FRR (SONiC) implement. The default `None` keeps the failure on FRR and Linux nodes. Generation drops targets whose vendor provider does not implement every listed operation, so do not keep a vendor-by-failure table.

SR Linux routers have no provider. A failure supports them through its `containerlab` branch, which it declares by listing `containerlab` in `supported_backends`. Without `supported_backends`, the failure runs on Kathara, and on Containerlab only with SONiC routers.

**Traffic and rate.** Leave `TRAFFIC_PROFILE` unset unless the symptom cannot show under the environment's baseline traffic (`BASELINE_TRAFFIC`, see [Baseline traffic in generated tasks](../operations/network-scenarios.md#baseline-traffic-in-generated-tasks)). A failure-specific profile makes the traffic itself a hint about the failure class. Set `minimum_path_rate_kbps` when the symptom appears only where the path forwards faster than the fault slows a flow, such as a CPU-starved receiver. Generation skips device profiles whose routers cap the path below that rate (`NetworkEnvBase.path_rate_limit_kbps`).

**Session metadata.** List instance attributes that should be recorded in the session's injection metadata in `RECORDED_ATTRS`.

**Benchmark targets.** `nika benchmark generate` has no per-failure code. The failure declares its targets:

- Override `benchmark_inject_params(ctx)` to return the canonical inject parameters for `ctx.scenario`. The default targets `ctx.host0`. Draw only from `ctx.rng`, so cases stay reproducible.
- Set `BENCHMARK_TARGETS` to `"node"`, `"link"`, or `"canonical"` to choose how variants are enumerated. Add `BENCHMARK_TARGET_ROLE`, `BENCHMARK_POINT_TO_POINT`, or `BENCHMARK_TUNNEL_IFACE` when they apply.
- Override `benchmark_inject_options`, `validate_benchmark_inject`, or `coordinate_benchmark_inject` (with `BENCHMARK_COORDINATES`) only for failure-specific enumeration, validation, or multi-failure pairing.

Shared target helpers live in [`problems/support/benchmark_targets.py`](../../src/nika/problems/support/benchmark_targets.py).

## Add traffic

Every environment runs always-on [business traffic](../operations/network-scenarios.md#business-traffic). Tasks add load through a [background traffic profile](../operations/network-scenarios.md#background-traffic-profiles). For one-off load during development, use the built-in generators.

OD-matrix iperf3 traffic:

```shell
nika traffic run od --all-to-host pc2 --mbps 20 --interval 60
nika traffic run od --mesh-mbps 5 --interval 300 --background
```

Web browsing traffic needs the scenario to define `web_urls` and web servers that `load_machines()` can discover:

```shell
nika traffic run web --pages-min 2 --pages-max 5 --no-loop
```

For faults that create traffic as their root cause, keep that logic inside the failure class. Use the traffic CLI or the generator APIs for background and validation traffic.

---

## Reference: traffic hooks

The profile planner reads only generic environment data: host nodes, `probe_intent()` isolation, and, for the `demand` pattern, `traffic_demands()`. Override `traffic_demands()` to return `{(src_host, dst_host): weight}` when your environment has a measured demand matrix.

When endpoint nodes have no Python, list them in `traffic_pod_nodes()` and start their programs in `start_traffic_programs()`. The Kubernetes environments do this with host-network pods ([`k8s_traffic_pods.py`](../../src/nika/net_env/utils/k8s_traffic_pods.py)).

Symptom probes find their endpoints through environment hooks, so they work on every router platform. Override `link_symptom_targets(node, intf)` to name the observer and destination for a link that the default probe path does not cross. Override `nat_probe_server()` to name the outside Linux server that NAT probes listen on.

## Add benchmark tasks

Benchmark YAML rows use the same names and injection parameters as the CLI:

```yaml
cases:
  - scenario: my_scenario
    topo_size: s
    problem: my_fault
    inject:
      host_name: pc1
      intf_name: eth0
  - scenario: my_scenario
    topo_size: s
    problem: healthy
    inject: {}
    root_causes: []
```

A multi-fault row uses `problems: [first_failure, second_failure]` and nests the injection parameters under each failure ID in `inject`. Benchmark execution turns these failures into persistent events at `0s` and starts diagnosis after the initial injections are verified. To give the agent or telemetry an observation interval between failures, declare distinct offsets with `events[].at` in [`schedule.events`](../benchmarks/scheduling.md#event-fields).

Regenerate the candidate catalog after you add a failure or a scenario, and refresh the failure-by-scenario tables in [Benchmark configuration](../benchmarks/benchmark-configuration.md):

```shell
uv run nika benchmark generate
uv run python scripts/render_coverage_matrix.py --write-docs
```

Review the YAML and docs table diffs.

Run a single task:

```shell
uv run nika benchmark run my_scenario --problem my_fault -s s \
  --set host_name=pc1 --set intf_name=eth0 \
  -a mock
```

Run a YAML file:

```shell
uv run nika benchmark run --config benchmark/my_cases.yaml \
  --result_dir results/my_cases \
  -a mock
```

> [!NOTE]
> Standalone multi-failure injection waits for a failure class's `MULTI_INJECT_DWELL_SEC` between injections (zero by default). Benchmark scheduling does not use that delay.

## Check the change

- `uv run nika env list` shows the scenario.
- `uv run nika env run <scenario>` deploys and creates a session.
- `uv run nika failure describe <problem>` shows the expected schema.
- `uv run nika failure inject <problem> --set ...` verifies successfully.
- `uv run nika benchmark run ... -a mock` completes without external LLM credentials.
- The session directory contains `ground_truth.json`, `run.json`, `nika.jsonl`, and the evaluation artifacts.
- If compatibility or target enumeration changes, regenerate the candidate pool (`nika benchmark generate`) and the coverage tables in [Benchmark configuration](../benchmarks/benchmark-configuration.md).

Update the [failure reference](../operations/failures.md) or the [network scenario reference](../operations/network-scenarios.md) in the same change when the registry or runtime behavior changes.

---

## Reference: validate device-profile cells

Declared compatibility says that a failure should work on an IOS XR, RouterOS, or SONiC deployment. A validation run shows that it does. `nika benchmark validate-profiles` covers every router platform whose failures run through a fault-operations provider: the Kathara native profiles and the Containerlab SONiC profile. It takes one representative case per cell of the working pool, where a cell is an (environment, `device_profile`, failure) combination. It prefers a case that targets a router of that platform. For each case it deploys the environment, injects the failure, checks `verify_fault` and the ground truth, runs `recover_fault` when the failure defines it, and closes the lab.

Before you run validation, prepare the vendor images with the [IOS XR setup](../operations/network-scenarios.md#ios-xr-simple-bgp-scenario) or the [RouterOS setup](../operations/network-scenarios.md#routeros-simple-bgp-scenario). RouterOS also needs `/dev/kvm`, with nested virtualization enabled when the host is a VM. SONiC cells use the [SONiC virtual switch image](../operations/network-scenarios.md#sonic-routers-on-containerlab), which `uv run nika images prepare` downloads. The validator skips a cell whose image is missing and records no result for it.

```shell
uv run nika benchmark validate-profiles --list                 # cells and last result
uv run nika benchmark validate-profiles --native-only          # cells that target native routers
uv run nika benchmark validate-profiles --environment dc_clos --device-profile routeros
uv run nika benchmark validate-profiles --environment min3clos --device-profile sonic
```

Each result goes to `benchmark/working/validation/device_profiles.yaml` as `validated`, `failed` (with the stage and error), or `unsupported` (a `RuntimeCapabilityError`). A record also holds the NIKA commit, the vendor image tag, and the date. Do not edit this file by hand. Cells run one at a time under `/tmp/nika-heavy-lab.lock`. Recorded cells are skipped, so an interrupted run resumes. Validated records without `recovery: restored` are rerun. Pass `--rerun` to repeat recorded cells. Run validation on demand or nightly, not for every change, because a native lab takes several minutes to boot. The base ISP scenarios share one capability surface, so they form a single `isp` environment.

The coverage matrix shows the declared compatibility and the last result for each cell. `nika benchmark generate --validated-only` keeps IOS XR, RouterOS, and SONiC cases only for validated cells.
