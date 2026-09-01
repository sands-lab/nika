# Create benchmark tasks

This guide is for NIKA contributors who add a network scenario, injectable failure, traffic source, or benchmark case. It follows the runtime and registry contracts used by the current implementation.

Core contracts: [`NetworkEnvBase`](../../src/nika/net_env/base.py), [`ProblemBase`](../../src/nika/problems/base.py), and the [`benchmark` workflows](../../src/nika/workflows/benchmark/).

## Understand the task model

A benchmark case combines:

- a network scenario from `src/nika/net_env/`
- either one injectable problem from `src/nika/problems/`, a `problems` list for coordinated multi-fault cases, or the sentinel `problem: healthy` (no fault)
- explicit injection parameters (empty for healthy cases; nested per problem for multi-fault rows)
- optional traffic generated before or during troubleshooting
- an agent run and evaluation output under `results/{session_id}/`

The standard pipeline is:

```shell
nika env run <scenario> [-s s|m|l]
nika failure inject <problem> [--set key=value ...]
nika failure inject <problem-a> <problem-b> [--set problem-a.field=value ...]
nika traffic run <type> ...
nika agent run -a <agent> ...
nika session close -y
nika eval metrics
```

For benchmark automation, `nika benchmark run` performs env deploy, fault injection, agent run, close, and eval for each case.

## Add a network scenario

Network environments implement `NetworkEnvBase` and bind to Kathara or Containerlab through the runtime layer. Read the [network scenario reference](../operations/network-scenarios.md) before adding another scenario ID; an existing parameterized scenario may already cover the topology.

1. Add a single-backend lab under `src/nika/net_env/<scenario>/`. Only multi-backend scenarios use backend subdirectories, such as `src/nika/net_env/isp/kathara/` and `src/nika/net_env/isp/containerlab/`. Shared backend helpers belong in `src/nika/net_env/utils/kathara/` or `src/nika/net_env/utils/containerlab/`.
2. Implement a class that sets `LAB_NAME`, initializes the backend lab/topology, sets `self.name`, `self.desc`, and declares useful host lists through `load_machines()`.
3. If the scenario has sizes, expose `TOPO_SIZE = ["s", "m", "l"]` and accept `topo_size` in `__init__`.
4. Add import-safe metadata and a lazy module/class binding to `src/nika/net_env/net_env_pool.py`.

Minimal shape:

```python
from Kathara.model.Lab import Lab
from Kathara.manager.Kathara import Kathara

from nika.net_env.base import NetworkEnvBase


class MyScenario(NetworkEnvBase):
    LAB_NAME = "my_scenario"
    TOPO_SIZE = ["s", "m", "l"]  # omit for fixed-size labs

    def __init__(self, topo_size: str = "s"):
        super().__init__()
        self.name = self.LAB_NAME
        self.desc = "Short operator-facing description."
        self.instance = Kathara.get_instance()
        self.lab = Lab(self.name)

        pc1 = self.lab.new_machine("pc1", image="nika/base")
        pc2 = self.lab.new_machine("pc2", image="nika/base")
        self.lab.connect_machine_to_link(pc1.name, "A")
        self.lab.connect_machine_to_link(pc2.name, "A")

        self.load_machines()
```

Register metadata without importing the backend package:

```python
"my_scenario": NetEnvSpec(
    lab_name="my_scenario",
    module="nika.net_env.kathara.example.my_scenario.lab",
    class_name="MyScenario",
    tags=("link", "icmp", "pc"),
    supported_backends=("kathara",),
    topo_size=["s", "m", "l"],
),
```

For a scenario with more than one backend, set `backend_bindings` to one `BackendEnvBinding` per backend. Keep the scenario ID and backend-neutral semantics the same across bindings.

Verify discovery and deployment:

```shell
uv run nika env list
uv run nika env run my_scenario -s s
uv run nika session inspect
uv run nika session close -y
```

Each SNDlib graph is its own scenario ID (`isp_abilene`, `isp_france`, …). Base ISP scenarios omit `-s` and still accept protocol flags. Kathara (FRR) and Containerlab (Nokia SR Linux) share the same IDs. RPKI and RTBH are named specials only:

```shell
uv run nika env run isp_abilene --igp isis
uv run nika env run isp_abilene --bgp-mode ibgp_rr
uv run nika env run isp_abilene --bgp-mode ebgp
uv run nika env run isp_france --backend containerlab --device-profile nokia_srlinux
uv run nika env run isp_abilene_ebgp_rpki
uv run nika env run isp_abilene_ebgp_rtbh
uv run nika traffic run sndlib --mode demands --max-intervals 1 --unit K --background
```

See the [SNDlib ISP scenarios reference](../operations/network-scenarios.md#sndlib-isp-scenarios) for the shared compiler, backend bindings, and control knobs.

## Add an injectable failure

Failures live under `src/nika/problems/<failure_domain>/`. The directory name must match the class `failure_domain`. Cross-domain base classes and helpers live under `src/nika/problems/support/`; support packages must not define registered failures. The registry discovers a concrete `ProblemBase` subclass when it sets `root_cause_name`, validates all taxonomy fields, and builds `META` during import. Put attacks, spoofing, and poisoning failures under `security`.

Each fault is a single `ProblemBase` subclass that implements injection, verification, and unified ground truth via `get_ground_truth()`. Do not split one fault into separate Detection / Localization / RCA classes.

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
```

Notes:

- Set `failure_domain`, `root_cause_name`, and `Params` on the class. `META` is auto-generated; you do not define it by hand.
- Place the implementation under the matching subsystem directory.
- `description` is a short explanation of what the failure ID means for the agent submission ontology (and preferred registry text). Name the root cause only. Do not include injection method, artifact names, verification evidence, or differential shortcuts such as "X fails while Y still works".
- `symptom_desc` is optional user-facing symptom text. When `description` is omitted, the registry description falls back to `symptom_desc`, then `root_cause_name`.
- `inject_fault()` should mutate only the selected lab instance.
- `verify_fault()` must prove the injection artifacts are present (nft/tc/config/process/quota). Failed verification marks the injection as failed and stops the run. Do not put slow network-impact probes in `verify_fault`; put those in the test-path API `tests.support.symptom.evaluate_symptom` (contracts under `tests/support/symptom/`).
- Implement `root_cause_resources(params)` so NIKA can derive structured RCA ground truth from injection parameters. Do not maintain a second root-cause table. Use `link_containing_endpoint` for controller-side cable faults (`link_down`, `link_flap`, `link_packet_corruption`); use `interface_on` when the mutated object is an interface. See [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).
- `Params` must be a Pydantic model. `nika failure describe` and benchmark YAML validation use it as the injection schema.

Verify the problem:

```shell
uv run nika failure list
uv run nika failure describe my_fault
uv run nika env run my_scenario -s s
uv run nika failure inject my_fault --set host_name=pc1 --set intf_name=eth0
uv run nika failure ps
```

## Generate traffic

Use the built-in traffic generators when a task needs load or baseline activity.

OD-matrix iperf3 traffic:

```python
import asyncio

from traffic.od_flows import ODFLowGenerator


async def run_traffic(lab_name: str):
    generator = ODFLowGenerator(lab_name=lab_name)
    return await generator.astart_generate_traffic(
        {"pc1": {"pc2": 20}},
        interval=60,
        unit="M",
        udp=True,
    )


asyncio.run(run_traffic("my_scenario__instance"))
```

CLI equivalent:

```shell
nika traffic run od --all-to-host pc2 --mbps 20 --interval 60
nika traffic run od --mesh-mbps 5 --interval 300 --background
```

Web browsing traffic requires the scenario to define `web_urls` and web servers discoverable by `load_machines()`:

```shell
nika traffic run web --pages-min 2 --pages-max 5 --no-loop
```

For faults that create traffic as the root cause, keep that logic inside the problem class. For background or validation traffic, prefer the traffic CLI or generator APIs.

## Add benchmark cases

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

Regenerate the candidate catalog and refresh the failure × scenario tables in [Benchmark configuration](../benchmarks/benchmark-configuration.md) after adding a failure or scenario:

```shell
uv run nika benchmark generate
uv run python scripts/render_coverage_matrix.py --write-docs
```

Review the YAML and docs table diffs.

Run a single case:

```shell
uv run nika benchmark run my_scenario --problem my_fault -s s \
  --set host_name=pc1 --set intf_name=eth0 \
  -a mock -m mock-v1
```

Run a YAML file:

```shell
uv run nika benchmark run --config benchmark/my_cases.yaml \
  --result_dir results/my_cases \
  -a mock -m mock-v1
```

## Validate the change

- `uv run nika env list` shows the scenario.
- `uv run nika env run <scenario>` deploys and creates a session.
- `uv run nika failure describe <problem>` shows the expected schema.
- `uv run nika failure inject <problem> --set ...` verifies successfully.
- `uv run nika benchmark run ... -a mock -m mock-v1` completes without external LLM credentials.
- The session directory contains `ground_truth.json`, `run.json`, `events.jsonl`, and evaluation artifacts.
- If compatibility or target enumeration changes, regenerate the candidate pool (`nika benchmark generate`) and the coverage tables in [Benchmark configuration](../benchmarks/benchmark-configuration.md).

Update the [failure reference](../operations/failures.md) or [network scenario reference](../operations/network-scenarios.md) in the same change when the registry or runtime behavior changes.
