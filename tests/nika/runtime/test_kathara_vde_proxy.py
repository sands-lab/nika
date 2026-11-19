"""Live contract for the dynamically inserted Kathara VDE fault proxy."""

from __future__ import annotations

import json
import statistics
import time
from unittest.mock import patch

import pytest

from nika.runtime.kathara.runtime import KatharaRuntime
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from tests.support.prerequisites import docker_available
from tests.support.simple_bgp.lab import SimpleBGP

pytestmark = pytest.mark.integration


def _peer_on_link(runtime: KatharaRuntime, host: str, intf: str) -> tuple[str, str]:
    """Return the peer node and interface for a point-to-point link."""
    _endpoint, peer = KatharaVdeFaultProxy(runtime)._link_ends(host, intf)
    return peer.node, peer.intf


def _wait_for_dhcp_host(runtime: KatharaRuntime, host: str, intf: str = "eth0") -> None:
    """Wait until a DHCP client has a global address and default route."""
    for _ in range(60):
        addr = runtime.exec(
            host, f"ip -4 -o addr show dev {intf} scope global 2>/dev/null || true"
        ).strip()
        route = runtime.exec(host, "ip route show default 2>/dev/null || true").strip()
        if addr and route:
            return
        time.sleep(2)
    raise AssertionError(f"{host}:{intf} did not obtain DHCP configuration")


def _throughput(runtime: KatharaRuntime, *, reverse: bool) -> float:
    rates = []
    for _ in range(2):
        runtime.exec("router2", "timeout 15 iperf3 -s -1 -p 42001 >/dev/null 2>&1 &")
        command = "iperf3 -c 193.10.11.2 -p 42001 -t 2 -J" + (" -R" if reverse else "")
        for attempt in range(5):
            result = json.loads(runtime.exec("router1", command, timeout=10))
            if "error" not in result:
                break
            time.sleep(0.2)
        assert "error" not in result, result
        rates.append(result["end"]["sum_received"]["bits_per_second"])
    return statistics.median(rates)


@pytest.mark.skipif(not docker_available(), reason="Docker required for VDE proxy")
def test_pre_ready_failure_resumes_the_owned_hub(tmp_path) -> None:
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.runtime.base import RuntimeCapabilityError
    from nika.runtime.factory import net_env_kwargs_for_session, resolve_backend
    from nika.runtime.kathara import vde_proxy
    from nika.runtime.meta import meta_path
    from nika.utils.session import Session
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session

    session_id = start_net_env("dc_clos", "s", result_dir=str(tmp_path), telemetry=[])
    try:
        session = Session().load_running_session(session_id=session_id)
        env = get_net_env_instance(
            session.scenario_name,
            lab_name=session.lab_name,
            backend=resolve_backend(session),
            **net_env_kwargs_for_session(session),
        )
        env.topology_file = meta_path(session, "topology_file", scenario_params=True)
        env.runtime_workdir = meta_path(
            session, "runtime_workdir", scenario_params=True
        )
        runtime = KatharaRuntime(env)
        address = runtime.get_data_plane_host_ip("web_1")
        assert address
        assert "0% packet loss" in runtime.exec(
            "client_1_1", f"ping -c 2 -W 2 {address}", timeout=15
        )
        proxy = KatharaVdeFaultProxy(runtime)
        target = '_PROCESS_HELPERS["send_pidfd"](switch_fd, signal.SIGSTOP)'
        assert vde_proxy._SPLICE_SOURCE.count(target) == 1
        source = vde_proxy._SPLICE_SOURCE.replace(
            target,
            target
            + '\n            print("HUB_PAUSED", flush=True)\n            time.sleep(60)',
        )
        with (
            patch.object(vde_proxy, "_SPLICE_SOURCE", source),
            patch.object(vde_proxy, "_READY_TIMEOUT_SEC", 6),
            pytest.raises(RuntimeCapabilityError, match="HUB_PAUSED"),
        ):
            proxy.insert("client_1_1", "eth0")
        assert proxy.discover("client_1_1", "eth0") is None
        assert "0% packet loss" in runtime.exec(
            "client_1_1", f"ping -c 2 -W 2 {address}", timeout=15
        )
    finally:
        close_session(session_id, undeploy=True, status="finished")


@pytest.mark.skipif(not docker_available(), reason="Docker required for VDE proxy")
def test_vde_proxy_preserves_point_to_point_link_identity_and_connectivity() -> None:
    """The splice leaves the lab interface itself untouched."""
    env = SimpleBGP(lab_name="test_vde_proxy")
    runtime = KatharaRuntime(env)
    state = None
    try:
        runtime.deploy()
        before = json.loads(runtime.exec("router1", "ip -j -s link show dev eth0"))[0]
        native_rates = [
            _throughput(runtime, reverse=reverse) for reverse in (False, True)
        ]
        proxy = KatharaVdeFaultProxy(runtime)
        state = proxy.insert("router1", "eth0")
        for reverse, baseline in zip((False, True), native_rates):
            rate = _throughput(runtime, reverse=reverse)
            assert 0.5 < rate / baseline < 2, (reverse, baseline, rate)
        after = json.loads(runtime.exec("router1", "ip -j link show dev eth0"))[0]
        for field in ("ifindex", "address", "mtu", "ifalias"):
            assert after[field] == before[field], field
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")
        assert "lladdr" in runtime.exec("router1", "ip neigh show 193.10.11.2")

        proxy.start_link_flap(state, 1, 1)
        assert proxy.link_flap_running(state)
        assert (
            runtime.exec("router1", "test -e /tmp/link_flap_eth0.pid; echo $?").strip()
            == "1"
        )
        # Both lab interfaces lose carrier, as on a failing cable.
        peer, peer_intf = state.peer.node, state.peer.intf
        sample = (
            "for i in $(seq 1 16); do cat /sys/class/net/{}/operstate; "
            "sleep 0.25; done | sort -u"
        )
        for node, intf in (("router1", "eth0"), (peer, peer_intf)):
            seen = set(runtime.exec(node, sample.format(intf), timeout=15).split())
            assert seen == {"up", "down"}, (node, intf, seen)
        proxy.stop_link_flap(state)
        for node, intf in (("router1", "eth0"), (peer, peer_intf)):
            link = json.loads(runtime.exec(node, f"ip -j link show dev {intf}"))[0]
            assert "LOWER_UP" in link["flags"], (node, link["flags"])

        proxy.set_netem_corrupt(state, 5)
        assert proxy.netem_configured(state)
        reverse_rate = _throughput(runtime, reverse=True)
        assert 0.5 < reverse_rate / native_rates[1] < 2
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
        # Recovery must also reclaim the native child and socket directory
        # when the controller relay cannot run its normal finally block.
        from nika.runtime.shared.processes import owned_pidfd, process_source

        state = proxy.insert("router1", "eth0")
        helper = proxy._client.containers.get(state.proxy_id)
        record = json.loads(helper.exec_run(["cat", "/run/splice.ready"]).output)
        native = record["native"]
        directory = record["directory"]
        cleanup_path = f"/proc/{native[0]}/root{directory}"
        # Pin the mount through the still-running original hub for readback.
        hub_path = f"/proc/{record['hub'][0]}/root{directory}"
        assert helper.exec_run(["test", "-d", cleanup_path]).exit_code == 0
        assert helper.exec_run(["test", "-d", hub_path]).exit_code == 0
        killed = helper.exec_run(
            [
                "python3",
                "-c",
                process_source() + "\nrecord = read_identity('/run/splice.ready')\n"
                "fd = owned_pidfd(record['relay'])\n"
                "send_pidfd(fd, signal.SIGKILL)\n",
            ]
        )
        assert killed.exit_code == 0, killed.output
        proxy._stop_splice(helper)
        assert helper.exec_run(["test", "-e", hub_path]).exit_code == 1
        proxy.remove(state)
        state = None
        assert owned_pidfd(native) is None
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")
        final = json.loads(runtime.exec("router1", "ip -j -s link show dev eth0"))[0]
        assert final["ifindex"] == before["ifindex"]
        # Counters continue across inject and recover instead of restarting.
        sent = before["stats64"]["tx"]["packets"]
        assert final["stats64"]["tx"]["packets"] >= sent + 8
    finally:
        if state is not None:
            KatharaVdeFaultProxy(runtime).remove(state, suppress_errors=True)
        runtime.destroy()


@pytest.mark.skipif(not docker_available(), reason="Docker required for VDE proxy")
def test_vde_proxy_preserves_bridge_slave_access_link() -> None:
    """Bridge-slave access ports stay on br0 and remain reachable before netem."""
    from nika.net_env.campus_lan.lab import CampusLan

    env = CampusLan(lab_name="test_vde_proxy_campus", topo_size="s")
    runtime = KatharaRuntime(env)
    state = None
    host = "pc_1_1_1_1"
    intf = "eth0"
    try:
        runtime.deploy()
        _wait_for_dhcp_host(runtime, host, intf)
        proxy = KatharaVdeFaultProxy(runtime)
        state = proxy.insert(host, intf)
        assert "0% packet loss" in runtime.exec(host, "ping -c 2 -W 2 10.200.0.3")

        peer_host, peer_intf = _peer_on_link(runtime, host, intf)
        peer_link = json.loads(
            runtime.exec(peer_host, f"ip -j link show dev {peer_intf}")
        )[0]
        assert peer_link.get("master") == "br0"

        proxy.set_netem_corrupt(state, 5)
        assert proxy.netem_corrupt_configured(state)
        proxy.remove(state)
        state = None
        assert "0% packet loss" in runtime.exec(host, "ping -c 2 -W 2 10.200.0.3")
    finally:
        if state is not None:
            KatharaVdeFaultProxy(runtime).remove(state, suppress_errors=True)
        runtime.destroy()
