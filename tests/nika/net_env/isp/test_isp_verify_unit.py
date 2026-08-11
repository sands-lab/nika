"""FakeRuntime unit tests for ISP verification."""

from __future__ import annotations

from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.kathara.isp.isp.verify import verify_isp_lab
from nika.topology.models import NetworkTopology, TopoLink, TopoNode


class FakeRuntime:
    def __init__(
        self,
        *,
        nodes: set[str] | None = None,
        overrides: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.nodes = nodes or set()
        self.overrides = overrides or {}

    def list_nodes(self) -> list[str]:
        return sorted(self.nodes)

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        key = (host, command)
        if key in self.overrides:
            return self.overrides[key]
        if command.startswith("systemctl is-active"):
            return "active"
        if "ip -4 -o addr show" in command:
            return "inet 10.0.0.0/31"
        if command.startswith("ping "):
            return "1 packets received"
        if "show isis neighbor" in command:
            # Enough Up neighbors for typical degree in tiny topo.
            return "Sys Name  Interface  L  State\npeer1 eth0 2 Up\n"
        if "show ip ospf neighbor" in command:
            return "Neighbor ID  Pri State\n1.1.1.1 1 Full/DR\n"
        return ""


def _plan(igp: str = "isis"):
    topo = NetworkTopology(
        name="tiny",
        source_format="sndlib-xml",
        meta={},
        nodes=(TopoNode(id="A"), TopoNode(id="B")),
        links=(TopoLink(id="L1", source="A", target="B"),),
        demands=(),
    )
    return compile_isp_plan(
        IspConfig(topology="polska", igp=igp),  # type: ignore[arg-type]
        topology=topo,
    )


def test_verify_success_isis() -> None:
    plan = _plan("isis")
    devices = {n.device_name for n in plan.nodes}
    overrides: dict[tuple[str, str], str] = {}
    for node in plan.nodes:
        overrides[(node.device_name, "systemctl is-active frr")] = "active"
        overrides[(node.device_name, "ip -4 -o addr show dev lo")] = (
            f"inet {node.loopback}/32"
        )
        if node.interfaces:
            iface = node.interfaces[0]
            overrides[(node.device_name, f"ip -4 -o addr show dev {iface.name}")] = (
                f"inet {iface.address}/{iface.prefixlen}"
            )
        overrides[(node.device_name, "vtysh -c 'show isis neighbor'")] = (
            "Sys  Iface  L  State\nx eth0 2 Up\n"
        )
        for other in plan.nodes:
            if other.device_name == node.device_name:
                continue
            overrides[(node.device_name, f"ping -c 1 -W 2 {other.loopback}")] = (
                "1 packets received"
            )
    runtime = FakeRuntime(nodes=devices, overrides=overrides)
    result = verify_isp_lab(runtime, plan=plan, scenario_name="isp")
    assert result["verified"]
    assert result["checks"]["nodes_deployed"]
    assert result["checks"]["igp_adjacencies"]
    assert result["checks"]["loopbacks_reachable"]
    assert result["details"]["inventory"]["node_count"] == 2


def test_verify_fails_when_nodes_missing() -> None:
    plan = _plan("isis")
    result = verify_isp_lab(
        FakeRuntime(nodes=set()),
        plan=plan,
        scenario_name="isp",
    )
    assert not result["verified"]
    assert not result["checks"]["nodes_deployed"]


def test_verify_fails_when_adjacency_missing() -> None:
    plan = _plan("isis")
    devices = {n.device_name for n in plan.nodes}
    overrides = {
        (n.device_name, "vtysh -c 'show isis neighbor'"): "empty" for n in plan.nodes
    }
    for node in plan.nodes:
        overrides[(node.device_name, "systemctl is-active frr")] = "active"
        overrides[(node.device_name, "ip -4 -o addr show dev lo")] = (
            f"inet {node.loopback}/32"
        )
        if node.interfaces:
            iface = node.interfaces[0]
            overrides[(node.device_name, f"ip -4 -o addr show dev {iface.name}")] = (
                f"inet {iface.address}/{iface.prefixlen}"
            )
        for other in plan.nodes:
            if other.device_name != node.device_name:
                overrides[(node.device_name, f"ping -c 1 -W 2 {other.loopback}")] = (
                    "1 packets received"
                )
    result = verify_isp_lab(
        FakeRuntime(nodes=devices, overrides=overrides),
        plan=plan,
        scenario_name="isp",
    )
    assert not result["verified"]
    assert not result["checks"]["igp_adjacencies"]
