"""Unit tests for nika.snapshot settings, routing and naming (no Incus needed)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from nika.remote.config import (
    RemoteConfig,
    is_remote_enabled,
    load_remote_config,
    remote_override,
)
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig, SnapshotSettings
from nika.snapshot import golden
from nika.snapshot.trial import trial_resource_names

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_run_config():
    reset_run_config()
    yield
    reset_run_config()


def test_snapshot_settings_default_off_and_validated() -> None:
    assert RunConfig().nika.snapshot.enabled is False
    with pytest.raises(ValidationError):
        SnapshotSettings(vm_memory="8G")
    with pytest.raises(ValidationError):
        SnapshotSettings(vm_memory_overrides={"isp_pdh": "20480MiB"})
    with pytest.raises(ValidationError):
        SnapshotSettings(base="nika-base")


def test_override_routes_lab_ops_and_maps_gateway_port() -> None:
    assert is_remote_enabled() is False
    cfg = RemoteConfig(
        enabled=True,
        url="http://127.0.0.1:41000",
        artifact_root="",
        gateway_ports={8800: 41001},
        gateway_host="host.docker.internal",
        disposable_lab=True,
    )
    with remote_override(cfg):
        assert is_remote_enabled() is True
        assert load_remote_config() is cfg
        assert cfg.gateway_url(8800) == "http://host.docker.internal:41001"
        with pytest.raises(RuntimeError):
            with remote_override(cfg):
                pass
    assert is_remote_enabled() is False


def test_disposable_lab_closes_without_undeploy() -> None:
    from nika.remote import workflows

    client = MagicMock()
    client.config.disposable_lab = True
    session = MagicMock(session_id="s1", session_dir="/tmp/s1")
    with (
        patch.object(workflows, "_client", return_value=client),
        patch.object(workflows, "Session", return_value=session),
        patch.object(workflows, "_pull_and_fix_local"),
    ):
        session.load_running_session.return_value = session
        workflows.remote_close_session("s1", undeploy=True)
    client.close_session.assert_called_once_with("s1", undeploy=False, stop_all=False)


def test_golden_key_tracks_variant_and_code(monkeypatch: pytest.MonkeyPatch) -> None:
    set_run_config(RunConfig())
    params = golden.variant(scenario="dc_clos", topo_size="s", igp=None)
    assert params == {"scenario": "dc_clos", "topo_size": "s"}
    monkeypatch.setattr(golden, "code_fingerprint", lambda: "a")
    key = golden.golden_key(params)
    assert key == golden.golden_key(dict(params))
    assert key != golden.golden_key({**params, "topo_size": "m"})
    monkeypatch.setattr(golden, "code_fingerprint", lambda: "b")
    assert key != golden.golden_key(params)


def test_trial_names_are_stable_and_fit_linux_bridges() -> None:
    project, bridge = trial_resource_names("dc_clos__link_down__t01__rabc")
    assert (project, bridge) == trial_resource_names("dc_clos__link_down__t01__rabc")
    assert project.startswith("nika-t-")
    assert len(bridge) <= 15  # IFNAMSIZ - 1
    assert trial_resource_names("other")[0] != project


def test_golden_build_liveness_protects_running_builds() -> None:
    import os
    import socket

    host = socket.gethostname()
    alive = {"user.nika.builder_pid": str(os.getpid()), "user.nika.host": host}
    assert golden._build_alive(alive) is True
    assert golden._build_alive({**alive, "user.nika.builder_pid": "0"}) is False
    # Builds on another host cannot be checked here; never treat them as abandoned.
    assert golden._build_alive({**alive, "user.nika.host": "elsewhere"}) is True


def test_remote_gateway_yields_the_port_this_host_dials() -> None:
    from nika.remote import workflows
    from nika.remote.client import RemoteClient
    from nika.remote.protocol import McpAttachResponse

    cfg = RemoteConfig(
        enabled=True,
        url="http://127.0.0.1:41000",
        artifact_root="",
        gateway_ports={8800: 41001},
    )
    client = RemoteClient(cfg)
    attach = McpAttachResponse(
        session_id="s1", gateway_port=8800, gateway_base_url="http://0.0.0.0:8800"
    )
    with (
        remote_override(cfg),
        patch.object(workflows, "_client", return_value=client),
        patch.object(client, "_request", return_value=attach.model_dump()),
        patch.object(client, "mcp_detach"),
    ):
        with workflows.remote_mcp_gateway("s1") as (url, port):
            # E2B tunnels dial (url host, port) from this host: both must point
            # at the local forwarder, not the lab VM's gateway port.
            assert (url, port) == ("http://127.0.0.1:41001", 41001)


def test_gateway_route_follows_the_sandbox_runtime() -> None:
    from nika.snapshot.trial import gateway_route

    # Host agents and E2B tunnels dial the forwarder on this host's loopback.
    assert gateway_route(None) == ("127.0.0.1", None)
    assert gateway_route("e2b") == ("127.0.0.1", None)
    # Docker Sandboxes reach this host through their bridge name.
    assert gateway_route("sbx") == ("0.0.0.0", "host.docker.internal")
