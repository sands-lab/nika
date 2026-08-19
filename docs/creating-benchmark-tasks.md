# Create benchmark tasks

This guide is for NIKA contributors who add a network scenario, injectable failure, traffic source, or benchmark case. It follows the runtime and registry contracts used by the current implementation.

Core contracts: [`NetworkEnvBase`](../src/nika/net_env/base.py), [`ProblemBase`](../src/nika/problems/problem_base.py), and the [`benchmark` workflows](../src/nika/workflows/benchmark/).

## Understand the task model

A benchmark case combines:

- a network scenario from `src/nika/net_env/`
- one injectable problem from `src/nika/problems/`
- explicit injection parameters
- optional traffic generated before or during troubleshooting
- an agent run and evaluation output under `results/{session_id}/`

The standard pipeline is:

```shell
nika env run <scenario> [-s s|m|l]
nika failure inject <problem> --set key=value ...
nika traffic run <type> ...
nika agent run -a <agent> ...
nika session close -y
nika eval metrics
```

For benchmark automation, `nika benchmark run` performs env deploy, fault injection, agent run, close, and eval for each case.

## Add a network scenario

Network environments implement `NetworkEnvBase` and bind to Kathara or Containerlab through the runtime layer. Read the [network scenario reference](network-scenarios.md) before adding another scenario ID; an existing parameterized scenario may already cover the topology.

1. Add the lab under `src/nika/net_env/kathara/<domain>/<scenario>/` (Kathara) or `src/nika/net_env/containerlab/<scenario>/` (Containerlab).
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

The ISP scenario is parameterized by SNDlib topology (not `-s`). The same `isp` scenario supports Kathara (FRR) and Containerlab (Nokia SR Linux):

```shell
uv run nika env run isp --topo polska --igp isis
uv run nika env run isp --topo polska --bgp-mode ibgp_rr
uv run nika env run isp --topo geant --bgp-mode ebgp
uv run nika env run isp --backend containerlab --device-profile nokia_srlinux --topo pdh
uv run nika traffic run sndlib --mode demands --max-intervals 1 --unit K --background
```

See the [`isp` scenario reference](network-scenarios.md#sndlib-isp-scenario) for the shared compiler, backend bindings, and control knobs. Enable offline RPKI/ROV with `--bgp-mode ebgp --rpki`.

## Add an injectable failure

Failures live under `src/nika/problems/<failure_domain>/`. The directory name must match the class `failure_domain`. Cross-domain base classes and helpers live under `src/nika/problems/support/`; support packages must not define registered failures. The registry discovers a concrete `ProblemBase` subclass when it sets `root_cause_name`, validates all taxonomy fields, and builds `META` during import.

Each fault is a single `ProblemBase` subclass that implements injection, verification, and unified ground truth via `get_ground_truth()`. Do not split one fault into separate Detection / Localization / RCA classes.

```python
from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    ProblemBase,
    build_verify_result,
)
from nika.problems.topology_inventory import interface_on


class MyFaultParams(BaseModel):
    host_name: str = Field(description="Target host.")
    intf_name: str = Field(default="eth0", description="Target interface.")


class MyFault(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    cause = FailureCause.HARDWARE
    symptom = FailureSymptom.DOWN
    scope = FailureScope.LINK
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "my_fault"
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

- Set `failure_domain`, `cause`, `symptom`, `scope`, `temporal`, `impact`, `root_cause_name`, and `Params` on the class. `META` is auto-generated; you do not define it by hand.
- Place the implementation under the matching subsystem directory. Configuration is a `cause` value, so do not create a `misconfigurations` domain or directory.
- `symptom_desc` is optional. When set, it becomes the registry description and ground-truth `detailed_cause`. When omitted, the registry description falls back to `root_cause_name`, while `detailed_cause` remains empty.
- `inject_fault()` should mutate only the selected lab instance.
- `verify_fault()` must prove the fault is active. Failed verification marks the injection as failed and stops the run.
- Implement `root_cause_resources(params)` so NIKA can derive structured RCA ground truth from injection parameters. Do not maintain a second root-cause table. See [Root-cause ground truth and scoring](root-cause-evaluation.md).
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

from nika.generator.traffic.od_flows import ODFLowGenerator


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
```

When the new failure or scenario should enter the working matrices, regenerate them and refresh the failure × scenario tables in [Benchmark configuration](benchmark-configuration.md) in the same change:

```shell
uv run python benchmark/generate_benchmark.py
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
- If working-matrix cases changed, `benchmark_full.yaml` / `benchmark_selected.yaml` and the coverage tables in [Benchmark configuration](benchmark-configuration.md) are updated together.

Update the [failure reference](failures.md) or [network scenario reference](network-scenarios.md) in the same change when the registry or runtime behavior changes.
