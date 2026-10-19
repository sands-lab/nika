"""Unit tests for nika.firecracker settings, routing and naming (no VM needed)."""

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
from nika.run_config.schema import RunConfig, FirecrackerSettings
from nika.firecracker import image
from nika.firecracker.trial import trial_dir

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_run_config():
    reset_run_config()
    yield
    reset_run_config()


def test_firecracker_settings_default_off_and_validated() -> None:
    assert RunConfig().nika.execution == "native"
    with pytest.raises(ValidationError):
        FirecrackerSettings(vm_memory="8G")
    with pytest.raises(ValidationError):
        FirecrackerSettings(vm_memory_overrides={"isp_pdh": "20480MiB"})


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


def test_image_key_tracks_variant_and_code(monkeypatch: pytest.MonkeyPatch) -> None:
    set_run_config(RunConfig())
    params = image.variant(scenario="dc_clos", topo_size="s", igp=None)
    assert params == {"scenario": "dc_clos", "topo_size": "s"}
    monkeypatch.setattr(image, "base_manifest", lambda: {"id": "base-1"})
    monkeypatch.setattr(image, "code_fingerprint", lambda: "a")
    key = image.image_key(params)
    assert key == image.image_key(dict(params))
    assert key != image.image_key({**params, "topo_size": "m"})
    monkeypatch.setattr(image, "code_fingerprint", lambda: "b")
    assert key != image.image_key(params)
    # A rebuilt base image gets a new id and invalidates every lab image.
    monkeypatch.setattr(image, "code_fingerprint", lambda: "a")
    monkeypatch.setattr(image, "base_manifest", lambda: {"id": "base-2"})
    assert key != image.image_key(params)


def test_trial_dirs_are_stable_per_trial() -> None:
    set_run_config(RunConfig())
    first = trial_dir("dc_clos__link_down__t01__rabc")
    assert first == trial_dir("dc_clos__link_down__t01__rabc")
    assert first != trial_dir("dc_clos__link_down__t02__rabc")
    assert first.parent.name == "trials"


def test_image_build_liveness_protects_running_builds() -> None:
    import os
    import socket

    host = socket.gethostname()
    alive = {"pid": os.getpid(), "host": host}
    assert image._builder_alive(alive) is True
    assert image._builder_alive({**alive, "pid": 0}) is False
    # Builds on another host cannot be checked here; never treat them as abandoned.
    assert image._builder_alive({**alive, "host": "elsewhere"}) is True


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
    from nika.firecracker.trial import gateway_route

    # Host agents and E2B tunnels dial the forwarder on this host's loopback.
    assert gateway_route(None) == ("127.0.0.1", None)
    assert gateway_route("e2b") == ("127.0.0.1", None)
    # Docker Sandboxes reach this host through their bridge name.
    assert gateway_route("sbx") == ("0.0.0.0", "host.docker.internal")


def test_guest_exec_frames_stdin_and_reports_status(tmp_path) -> None:
    """The host side of nika-guest-exec: vsock handshake, sized stdin, exit status."""
    import base64
    import socket
    import threading

    from nika.firecracker import vmm

    uds = tmp_path / "vsock.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(uds))
    server.listen(2)

    def fake_guest() -> None:
        for _ in range(2):
            conn, _ = server.accept()
            stream = conn.makefile("rb")
            assert stream.readline() == f"CONNECT {vmm.EXEC_PORT}\n".encode()
            conn.sendall(b"OK 1\n")
            script = base64.b64decode(stream.readline()).decode()
            data = stream.read(int(stream.readline()))
            status = 0 if script == "cat" else 3
            # A background job may still print after the status line.
            conn.sendall(data + f"\n__nika_exit={status}\nlate output\n".encode())
            conn.shutdown(socket.SHUT_RDWR)  # the makefile() keeps the socket open
            conn.close()

    threading.Thread(target=fake_guest, daemon=True).start()
    assert vmm.guest_exec(uds, "cat", stdin=b"payload") == "payload"
    with pytest.raises(vmm.VmmError, match="failed \\(3\\)"):
        vmm.guest_exec(uds, "false")


def test_page_server_prefetches_only_data_extents(tmp_path) -> None:
    """Holes in a sparse lab image memory file are not copied into the guest."""
    import os

    from nika.firecracker.uffd import _data_extents

    path = tmp_path / "mem"
    mib = 1 << 20
    with open(path, "wb") as fh:
        fh.truncate(64 * mib)
        for offset in (0, 32 * mib):
            fh.seek(offset)
            fh.write(b"x" * mib)
    with open(path, "rb") as fh:
        extents = list(_data_extents(fh.fileno()))
    # Filesystems round extents to their block size; the data must be inside them.
    assert len(extents) == 2
    assert extents[0][0] == 0 and extents[0][1] >= mib
    assert extents[1][0] <= 32 * mib < extents[1][1] < 64 * mib
    assert sum(end - start for start, end in extents) < 8 * mib
    assert os.path.getsize(path) == 64 * mib


def test_scheduler_groups_trials_by_variant() -> None:
    from types import SimpleNamespace

    from nika.firecracker.schedule import group_by_variant

    set_run_config(RunConfig())
    rows = [
        {"scenario": "dc_clos", "topo_size": "s", "problem": "a"},
        {"scenario": "campus_lan", "topo_size": "s", "problem": "b"},
        {"scenario": "dc_clos", "topo_size": "s", "problem": "c"},
        {"scenario": "dc_clos", "topo_size": "m", "problem": "d"},
    ]
    ordered = group_by_variant([SimpleNamespace(row=r) for r in rows])
    assert [t.row["problem"] for t in ordered] == ["a", "c", "b", "d"]


def test_hugepage_need_reserves_the_whole_vm() -> None:
    from nika.firecracker import schedule

    row = {"scenario": "dc_clos", "topo_size": "s"}
    set_run_config(
        RunConfig.model_validate({"nika": {"firecracker": {"huge_pages": False}}})
    )
    assert schedule.hugepage_need_mib(row) == 0
    set_run_config(
        RunConfig.model_validate(
            {
                "nika": {
                    "firecracker": {
                        "huge_pages": True,
                        "vm_memory": "1GiB",
                        "vm_memory_overrides": {"k8s_lab": "8GiB"},
                    }
                }
            }
        )
    )
    assert schedule.hugepage_need_mib(row) == 1024
    assert schedule.hugepage_need_mib({"scenario": "k8s_lab"}) == 8192


def test_firecracker_runs_have_no_heavy_tier() -> None:
    from types import SimpleNamespace

    from nika.workflows.benchmark.admit import CLASS_LIGHT, resource_class

    trial = SimpleNamespace(row={"scenario": "k8s_lab"}, case_key="k")
    set_run_config(RunConfig())
    assert resource_class(trial) != CLASS_LIGHT
    set_run_config(RunConfig.model_validate({"nika": {"execution": "firecracker"}}))
    assert resource_class(trial) == CLASS_LIGHT
