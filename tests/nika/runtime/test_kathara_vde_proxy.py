"""Live contract for the dynamically inserted Kathara VDE fault proxy."""

from __future__ import annotations

import json

import pytest

from nika.runtime.kathara.runtime import KatharaRuntime
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from tests.support.prerequisites import docker_available
from tests.support.simple_bgp.lab import SimpleBGP


@pytest.mark.skipif(not docker_available(), reason="Docker required for VDE proxy")
def test_vde_proxy_preserves_point_to_point_link_identity_and_connectivity() -> None:
    """A split LAN plus bridge is transparent before netem is enabled."""
    env = SimpleBGP(lab_name="test_vde_proxy")
    runtime = KatharaRuntime(env)
    state = None
    try:
        runtime.deploy()
        before = json.loads(runtime.exec("router1", "ip -j link show dev eth0"))[0]
        proxy = KatharaVdeFaultProxy(runtime)
        state = proxy.insert("router1", "eth0")
        after = json.loads(runtime.exec("router1", "ip -j link show dev eth0"))[0]
        assert after["ifname"] == "eth0"
        assert after["address"] == before["address"]
        assert after["mtu"] == before["mtu"]
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")
        assert "lladdr" in runtime.exec("router1", "ip neigh show 193.10.11.2")

        proxy.start_link_flap(state, 1, 1)
        assert proxy.link_flap_running(state)
        assert (
            runtime.exec("router1", "test -e /tmp/link_flap_eth0.pid; echo $?").strip()
            == "1"
        )
        proxy.stop_link_flap(state)

        proxy.set_netem_corrupt(state, 5)
        assert proxy.netem_configured(state)
        assert "netem" not in runtime.exec("router1", "tc qdisc show dev eth0")
        proxy.remove(state)
        state = None
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")

        state = proxy.insert("router1", "eth0")
        proxy.set_tbf(state, rate="30kbit", burst="64kb", limit="500kb")
        assert proxy.tbf_configured(state)
        assert "tbf" not in runtime.exec("router1", "tc qdisc show dev eth0").lower()
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")
        proxy.remove(state)
        state = None
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")
    finally:
        if state is not None:
            KatharaVdeFaultProxy(runtime).remove(state, suppress_errors=True)
        runtime.destroy()
