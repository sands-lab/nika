"""Session close removes owned VDE TAPs without touching sibling sessions."""

from __future__ import annotations

import json
import subprocess
from uuid import uuid4

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.runtime.kathara.tap import owned_host_taps, tap_owner
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(not docker_available(), reason="Docker required for VDE TAPs")
def test_close_cleans_detached_tap_and_preserves_sibling_session(tmp_path) -> None:
    sessions = []
    token = uuid4().hex[:8]
    try:
        for tag in ("target", "sibling"):
            sid = start_net_env(
                "simple_bgp",
                None,
                instance_tag=f"tap-{tag}-{token}",
                result_dir=str(tmp_path),
            )
            sessions.append(sid)
        target, sibling = [SessionStore().get_session(sid) for sid in sessions]
        env = get_net_env_instance("simple_bgp", lab_name=target["lab_name"])
        runtime = env._build_runtime()
        assert target["metadata"]["tap_owner"] == tap_owner(target["lab_name"])
        before = json.loads(runtime.exec("router1", "ip -d -j link show eth0"))[0]
        assert before["ifalias"] == target["metadata"]["tap_owner"]
        assert "0% packet loss" in runtime.exec("router1", "ping -c 2 -W 2 193.10.11.2")
        peer_env = get_net_env_instance("simple_bgp", lab_name=sibling["lab_name"])
        peer_runtime = peer_env._build_runtime()

        # Strand actual session TAPs on the host to exercise the cleanup path.
        for owner_runtime, suffix in ((runtime, "a"), (peer_runtime, "b")):
            container = owner_runtime.get_container("pc2")
            container.reload()
            name = f"tap{token}{suffix}"
            owner_runtime.exec(
                "pc2", f"ip link set eth0 down; ip link set eth0 name {name}"
            )
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "nsenter",
                    "-t",
                    str(container.attrs["State"]["Pid"]),
                    "-n",
                    "ip",
                    "link",
                    "set",
                    name,
                    "netns",
                    "1",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        assert owned_host_taps(target["lab_name"])
        sibling_taps = owned_host_taps(sibling["lab_name"])
        assert sibling_taps
        close_session(session_id=sessions[0])
        assert not owned_host_taps(target["lab_name"])
        assert owned_host_taps(sibling["lab_name"]) == sibling_taps
        with pytest.raises(FileNotFoundError):
            SessionStore().get_session(sessions[0])

        assert "0% packet loss" in peer_runtime.exec(
            "router1", "ping -c 2 -W 2 193.10.11.2"
        )
        assert not peer_runtime.interface_exists("pc2", "eth0")
        close_session(session_id=sessions[1])
        assert not peer_runtime.has_leftover_resources()
    finally:
        errors = []
        for sid in sessions:
            try:
                close_session(session_id=sid)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Session cleanup failed", errors)
