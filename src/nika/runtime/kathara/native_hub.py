"""Install NIKA's native hub in a fresh, isolated Firecracker guest."""

from __future__ import annotations

import io
import os
import platform
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.shared.containers import docker_client

_GUEST_EXEC = Path("/usr/local/bin/nika-guest-exec")


def prepare_guest_native_hub() -> dict[str, Any]:
    """Replace the idle guest plugin's hub before deploying its first environment."""
    if not _GUEST_EXEC.is_file():
        raise RuntimeError("Native hub preparation requires a NIKA Firecracker guest")
    client = docker_client()
    plugins = [
        plugin
        for plugin in client.plugins.list()
        if plugin.attrs.get("Enabled")
        and plugin.attrs.get("PluginReference", "")
        .removeprefix("docker.io/")
        .startswith("kathara/katharanp_vde:")
    ]
    if not plugins:
        return {"plugins": []}
    # The image builder owns a fresh guest; never replace a hub used by a network.
    networks = client.networks.list()
    if any(n.attrs["Driver"] not in {"bridge", "host", "null"} for n in networks):
        raise RuntimeError("Native hub preparation requires an undeployed guest")
    image = client.images.get(nika_image("vde-switch"))
    helper = client.containers.create(image.id, network_mode="none")
    try:
        stream, _ = helper.get_archive("/vde_switch")
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
            member = archive.getmember("vde_switch")
            if not member.isfile():
                raise RuntimeError("Native hub image must contain a regular binary")
            binary = archive.extractfile(member).read()
    finally:
        helper.remove(force=True)
    machine = {"x86_64": 62, "aarch64": 183}.get(platform.machine())
    if (
        machine is None
        or binary[:6] != b"\x7fELF\x02\x01"
        or int.from_bytes(binary[18:20], "little") != machine
    ):
        raise RuntimeError("Native hub binary does not match the guest architecture")
    root = Path(client.info()["DockerRootDir"]) / "plugins"
    receipts = []
    for plugin in plugins:
        plugin_id = plugin.attrs["Id"]
        if not re.fullmatch(r"[0-9a-f]{64}", plugin_id):
            raise RuntimeError("Invalid managed network plugin ID")
        target = root / plugin_id / "rootfs/usr/local/bin/vde_switch"
        if target.read_bytes() != binary:
            fd, temporary = tempfile.mkstemp(prefix=".nika-vde-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(binary)
                    file.flush()
                    os.fsync(file.fileno())
                os.chmod(temporary, target.stat().st_mode & 0o777)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        receipts.append(plugin_id)
    return {"image_id": image.id, "plugins": receipts}
