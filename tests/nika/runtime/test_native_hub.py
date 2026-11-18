"""Guest hub preparation preserves active networks and cleans up extraction helpers."""

import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.runtime.kathara import native_hub

pytestmark = pytest.mark.unit


@pytest.fixture
def guest(tmp_path, monkeypatch):
    marker = tmp_path / "nika-guest-exec"
    marker.touch()
    monkeypatch.setattr(native_hub, "_GUEST_EXEC", marker)
    monkeypatch.setattr(native_hub.platform, "machine", lambda: "x86_64")
    plugin_id = "a" * 64
    target = tmp_path / "plugins" / plugin_id / "rootfs/usr/local/bin/vde_switch"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original native hub")
    target.chmod(0o755)
    binary = b"\x7fELF\x02\x01" + bytes(12) + (62).to_bytes(2, "little") + bytes(80)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("vde_switch")
        member.size = len(binary)
        archive.addfile(member, io.BytesIO(binary))
    helper = Mock()
    helper.get_archive.return_value = ([buffer.getvalue()], {})
    client = Mock()
    client.plugins.list.return_value = [
        SimpleNamespace(
            attrs={
                "Enabled": True,
                "PluginReference": "docker.io/kathara/katharanp_vde:amd64",
                "Id": plugin_id,
            }
        )
    ]
    client.networks.list.return_value = [SimpleNamespace(attrs={"Driver": "bridge"})]
    client.containers.create.return_value = helper
    client.info.return_value = {"DockerRootDir": str(tmp_path)}
    client.images.get.return_value = SimpleNamespace(id="sha256:native-image")
    monkeypatch.setattr(native_hub, "docker_client", lambda: client)
    return SimpleNamespace(
        client=client, helper=helper, binary=binary, target=target, marker=marker
    )


def test_prepare_fresh_guest_records_image_identity(guest):
    result = native_hub.prepare_guest_native_hub()
    assert guest.target.read_bytes() == guest.binary
    assert guest.target.stat().st_mode & 0o777 == 0o755
    assert result == {
        "image_id": "sha256:native-image",
        "plugins": ["a" * 64],
    }
    guest.helper.remove.assert_called_once_with(force=True)


def test_prepare_requires_guest_marker_before_accessing_docker(guest):
    guest.marker.unlink()
    with pytest.raises(RuntimeError, match="Firecracker guest"):
        native_hub.prepare_guest_native_hub()
    guest.client.plugins.list.assert_not_called()


def test_prepare_refuses_deployed_plugin_network(guest):
    original = guest.target.read_bytes()
    guest.client.networks.list.return_value = [
        SimpleNamespace(attrs={"Driver": "kathara/katharanp_vde:amd64"})
    ]
    with pytest.raises(RuntimeError, match="undeployed guest"):
        native_hub.prepare_guest_native_hub()
    assert guest.target.read_bytes() == original
    guest.client.containers.create.assert_not_called()


def test_prepare_does_not_change_other_network_plugins(guest):
    guest.client.plugins.list.return_value[0].attrs["PluginReference"] = (
        "kathara/katharanp:amd64"
    )
    assert native_hub.prepare_guest_native_hub() == {"plugins": []}
    guest.client.images.get.assert_not_called()


def test_prepare_rejects_foreign_binary_and_removes_helper(guest, monkeypatch):
    monkeypatch.setattr(native_hub.platform, "machine", lambda: "aarch64")
    original = guest.target.read_bytes()
    with pytest.raises(RuntimeError, match="architecture"):
        native_hub.prepare_guest_native_hub()
    assert guest.target.read_bytes() == original
    guest.helper.remove.assert_called_once_with(force=True)


def test_prepare_cleans_helper_after_failed_archive_read(guest):
    guest.helper.get_archive.side_effect = RuntimeError("archive unavailable")
    with pytest.raises(RuntimeError, match="archive unavailable"):
        native_hub.prepare_guest_native_hub()
    guest.helper.remove.assert_called_once_with(force=True)


def test_failed_atomic_replacement_preserves_native_binary(guest, monkeypatch):
    original = guest.target.read_bytes()
    monkeypatch.setattr(
        native_hub.os, "replace", Mock(side_effect=OSError("read-only"))
    )
    with pytest.raises(OSError, match="read-only"):
        native_hub.prepare_guest_native_hub()
    assert guest.target.read_bytes() == original
    assert list(guest.target.parent.iterdir()) == [guest.target]


@pytest.mark.parametrize(
    "receipt", [None, {"image_id": "sha256:hub", "plugins": ["a" * 64]}]
)
def test_restored_trial_retains_native_hub_identity(tmp_path, monkeypatch, receipt):
    from nika.firecracker.trial import _record_trial_timings

    if receipt:
        (tmp_path / "meta.json").write_text(json.dumps({"native_hub": receipt}))
    store = Mock()
    monkeypatch.setattr("nika.utils.session_store.SessionStore", lambda: store)
    event = Mock()
    monkeypatch.setattr("nika.utils.logger.log_event", event)
    image = SimpleNamespace(key="lab-image", path=tmp_path)
    _record_trial_timings("trial-session", image, {"ready_sec": 1})
    saved = store.update_session.call_args.args[1]["firecracker"]
    assert saved["image"] == "lab-image"
    assert saved.get("native_hub") == receipt
    assert event.call_args.kwargs.get("native_hub") == receipt
