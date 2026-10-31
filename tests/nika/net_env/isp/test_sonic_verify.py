"""SONiC ISP verify parsers against supervisorctl, kernel, and APPL_DB output."""

from __future__ import annotations

import pytest

from nika.net_env.isp.bgp.plan import BgpOriginatedPrefix, BgpPlan, BgpSession
from nika.net_env.isp.containerlab.verify_sonic import (
    _addresses_ok,
    _appl_routes_ok,
    _bgp_data_plane_probes,
    _services_ok,
)
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.utils.sonic.render import SonicRouter
from nika.topology.models import NetworkTopology, TopoLink, TopoNode

pytestmark = pytest.mark.unit


class FakeRuntime:
    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        json_outputs: dict | None = None,
    ) -> None:
        self.outputs = outputs or {}
        self.json_outputs = json_outputs or {}

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        del command, timeout
        return self.outputs.get(host, "")

    def json(self, host: str, command: str):
        del command
        value = self.json_outputs[host]
        if isinstance(value, BaseException):
            raise value
        return value


def _tiny_plan():
    topology = NetworkTopology(
        name="tiny",
        source_format="sndlib-xml",
        meta={},
        nodes=(TopoNode(id="A"), TopoNode(id="B")),
        links=(TopoLink(id="L1", source="A", target="B"),),
        demands=(),
    )
    return compile_isp_plan(IspConfig(topology="polska", igp="isis"), topology=topology)


def _router(name: str, **addresses: tuple[str, ...]) -> SonicRouter:
    return SonicRouter(
        name=name,
        frr_conf=f"hostname {name}\nrouter bgp 65001\n",
        addresses=addresses,
    )


def _supervisor(*rows: tuple[str, str]) -> str:
    lines = []
    for name, state in rows:
        if state == "RUNNING":
            lines.append(f"{name:32} RUNNING   pid 42, uptime 0:01:00")
        else:
            lines.append(f"{name:32} {state}     Exited too quickly")
    return "\n".join(lines)


def _running(*names: str) -> str:
    return _supervisor(*[(name, "RUNNING") for name in names])


BOOT = ("orchagent", "syncd", "zebra", "fpmsyncd", "bgpd")


def test_services_ok_requires_boot_and_protocol_daemons_running() -> None:
    routers = {"r1": _router("r1")}
    healthy = FakeRuntime({"r1": _running(*BOOT)})
    assert _services_ok(healthy, routers)
    missing_bgpd = FakeRuntime(
        {"r1": _running("orchagent", "syncd", "zebra", "fpmsyncd")}
    )
    assert not _services_ok(missing_bgpd, routers)
    fatal = FakeRuntime(
        {
            "r1": _supervisor(
                ("orchagent", "FATAL"),
                ("syncd", "RUNNING"),
                ("zebra", "RUNNING"),
                ("fpmsyncd", "RUNNING"),
                ("bgpd", "RUNNING"),
            )
        }
    )
    assert not _services_ok(fatal, routers)


def test_addresses_ok_matches_kernel_ipv4_and_skips_ipv6() -> None:
    routers = {
        "r1": _router(
            "r1",
            Ethernet0=("10.0.0.0/31",),
            Loopback0=("10.255.0.1/32", "2001:db8::1/128"),
        )
    }
    kernel = [
        {
            "ifname": "Ethernet0",
            "addr_info": [{"local": "10.0.0.0", "prefixlen": 31}],
        },
        {
            "ifname": "Loopback0",
            "addr_info": [{"local": "10.255.0.1", "prefixlen": 32}],
        },
    ]
    assert _addresses_ok(FakeRuntime(json_outputs={"r1": kernel}), routers)
    missing = [kernel[0]]
    assert not _addresses_ok(FakeRuntime(json_outputs={"r1": missing}), routers)
    assert not _addresses_ok(
        FakeRuntime(json_outputs={"r1": ValueError("bad json")}), routers
    )


def test_appl_db_host_routes_without_slash32_satisfy_igp_loopbacks() -> None:
    plan = _tiny_plan()
    a, b = plan.nodes
    present = {
        a.device_name: f"ROUTE_TABLE:{b.loopback}\nROUTE_TABLE:10.0.0.0/31\n",
        b.device_name: f"ROUTE_TABLE:{a.loopback}\n",
    }
    assert _appl_routes_ok(FakeRuntime(present), plan, None)
    missing = dict(present)
    missing[a.device_name] = "ROUTE_TABLE:10.0.0.0/31\n"
    assert not _appl_routes_ok(FakeRuntime(missing), plan, None)

    prefix = "198.51.100.0/24"
    bgp = BgpPlan(
        mode="ebgp",
        topology_name=plan.topology_name,
        nodes=(),
        sessions=(),
        originated=(),
        expect_reachable=((a.device_name, prefix),),
        deny_prefixes=(),
        inventory={},
    )
    without_bgp = dict(present)
    assert not _appl_routes_ok(FakeRuntime(without_bgp), plan, bgp)
    with_bgp = dict(present)
    with_bgp[a.device_name] += f"ROUTE_TABLE:{prefix}\n"
    assert _appl_routes_ok(FakeRuntime(with_bgp), plan, bgp)


def test_bgp_data_plane_probe_sources_from_ebgp_link_to_origin() -> None:
    origin = BgpOriginatedPrefix(
        device="r2", prefix="198.51.110.0/24", ping_address="198.51.110.1"
    )
    plan = BgpPlan(
        mode="ebgp",
        topology_name="tiny",
        nodes=(),
        sessions=(
            BgpSession(
                local_device="r1",
                remote_device="r2",
                local_ip="10.0.0.0",
                remote_ip="10.0.0.1",
                local_asn=65001,
                remote_asn=65002,
                session_type="ebgp",
                update_source=None,
            ),
            BgpSession(
                local_device="r3",
                remote_device="r2",
                local_ip="10.255.0.3",
                remote_ip="10.255.0.2",
                local_asn=65002,
                remote_asn=65002,
                session_type="ibgp",
                update_source="lo",
            ),
        ),
        originated=(origin,),
        expect_reachable=(
            ("r1", origin.prefix),
            ("r3", origin.prefix),
            ("r1", "203.0.113.0/24"),
        ),
        deny_prefixes=(),
        inventory={},
    )
    probes = _bgp_data_plane_probes(plan)
    assert probes == [
        ("r1", "ping -I 10.0.0.0 -c 3 -W 2 198.51.110.1"),
        ("r3", "ping -c 3 -W 2 198.51.110.1"),
    ]
