"""Session close removes stopped Containerlab nodes after topology loss."""

from __future__ import annotations

from pathlib import Path
import subprocess
from uuid import uuid4

import docker
import pytest

from nika.runtime.factory import runtime_for_session
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.prerequisites import containerlab_prerequisites, docker_available

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(
    not docker_available() or not containerlab_prerequisites(),
    reason="Docker, Containerlab and gnmic required",
)
def test_close_cleans_stopped_nodes_without_topology_and_preserves_sibling(tmp_path):
    client = docker.from_env()
    session_id = None
    sibling = None
    sibling_namespace = None
    sibling_target = None
    sibling_link_inode = None
    try:
        session_id = start_net_env("min3clos", None, result_dir=str(tmp_path))
        meta = SessionStore().get_session(session_id)
        runtime = runtime_for_session(meta)
        selector = {"label": f"containerlab={meta['lab_name']}"}
        nodes = client.containers.list(all=True, filters=selector)
        assert nodes
        namespaces = [Path("/var/run/netns") / node.name for node in nodes]
        assert all(path.is_symlink() for path in namespaces)
        sibling_lab = f"{meta['lab_name']}-sibling"
        sibling = client.containers.create(
            "nika/base:0.2.0",
            command=["sleep", "300"],
            name=f"clab-{sibling_lab}-{uuid4().hex[:8]}",
            network_mode="none",
            labels={"containerlab": sibling_lab},
        )
        sibling.start()
        sibling.reload()
        sibling_pid = sibling.attrs["State"]["Pid"]
        assert sibling_pid > 1
        namespace_inodes = subprocess.check_output(
            [
                "sudo",
                "-n",
                "stat",
                "-Lc",
                "%i",
                f"/proc/{sibling_pid}/ns/net",
                "/proc/1/ns/net",
            ],
            text=True,
            timeout=10,
        ).split()
        assert len(namespace_inodes) == 2
        assert namespace_inodes[0] != namespace_inodes[1]
        sibling_namespace = Path("/var/run/netns") / sibling.name
        sibling_target = Path(f"/proc/{sibling_pid}/ns/net")
        subprocess.run(
            [
                "sudo",
                "-n",
                "ln",
                "-s",
                "--",
                str(sibling_target),
                str(sibling_namespace),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        sibling_link_inode = sibling_namespace.lstat().st_ino
        for node in nodes:
            node.stop(timeout=10)
        # Reproduce an interrupted close whose topology was already removed.
        Path(meta["topology_file"]).unlink()
        assert not runtime.exists()
        assert runtime.has_leftover_resources()
        close_session(session_id=session_id)
        assert not client.containers.list(all=True, filters=selector)
        assert not runtime.has_leftover_resources()
        assert not any(path.is_symlink() for path in namespaces)
        sibling.reload()
        assert sibling.status == "running"
        assert sibling_namespace.is_symlink()
        assert sibling_namespace.readlink() == sibling_target
        assert sibling_namespace.lstat().st_ino == sibling_link_inode
        with pytest.raises(FileNotFoundError):
            SessionStore().get_session(session_id)
    finally:
        try:
            if session_id is not None:
                close_session(session_id=session_id)
        finally:
            if sibling is not None:
                try:
                    if (
                        sibling_namespace is not None
                        and sibling_namespace.is_symlink()
                        and sibling_namespace.readlink() == sibling_target
                        and sibling_namespace.lstat().st_ino == sibling_link_inode
                    ):
                        subprocess.run(
                            ["sudo", "-n", "unlink", "--", str(sibling_namespace)],
                            check=True,
                            capture_output=True,
                            timeout=10,
                        )
                finally:
                    sibling.remove(force=True, v=True)
