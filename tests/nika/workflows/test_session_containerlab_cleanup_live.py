"""Session close removes stopped Containerlab nodes after topology loss."""

from __future__ import annotations

from pathlib import Path
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
    try:
        session_id = start_net_env("min3clos", None, result_dir=str(tmp_path))
        meta = SessionStore().get_session(session_id)
        runtime = runtime_for_session(meta)
        selector = {"label": f"containerlab={meta['lab_name']}"}
        nodes = client.containers.list(all=True, filters=selector)
        assert nodes
        namespaces = [Path("/var/run/netns") / node.name for node in nodes]
        assert all(path.is_symlink() for path in namespaces)
        sibling = client.containers.create(
            "nika/base:0.2.0",
            command="true",
            network_mode="none",
            labels={"containerlab": f"cleanup-sibling-{uuid4().hex}"},
        )
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
        assert sibling.status == "created"
        with pytest.raises(FileNotFoundError):
            SessionStore().get_session(session_id)
    finally:
        try:
            if session_id is not None:
                close_session(session_id=session_id)
        finally:
            if sibling is not None:
                sibling.remove(v=True)
