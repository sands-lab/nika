"""Controller-built eBPF injector for switch-internal packet corruption."""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
from pathlib import Path

from nika.net_env.utils.kathara.docker_files.docker_images import (
    ensure_nika_docker_images,
    nika_image,
)
from nika.runtime.base import RuntimeCapabilityError

TC_BPF_IMAGE = nika_image("tc-bpf")
# platform.machine() -> libbpf __TARGET_ARCH_* suffix.
BPF_TARGET_ARCH = {"x86_64": "x86", "aarch64": "arm64"}


class SwitchNamespaceBitflip:
    """Attach the failure's TC classifier without exposing build state to agents."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def attach(self, node: str, intf: str, seed: int) -> str:
        token = hashlib.blake2s(
            f"{self.runtime.lab_name}:{node}:{intf}:{seed}".encode(), digest_size=8
        ).hexdigest()
        object_name = f".dp-{token}"
        source = Path(__file__).with_name("switch_internal_corruption.bpf.c")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(source, arcname=f"{object_name}.c")
        # nika/tc-bpf is built for the host architecture.
        machine = platform.machine()
        # Lab images ship tc without libelf or clang, so a short-lived loader
        # container builds the object and joins the node's network namespace
        # to attach it.
        ensure_nika_docker_images([TC_BPF_IMAGE])
        node_container = self.runtime.get_container(node)
        loader = node_container.client.containers.create(
            TC_BPF_IMAGE,
            command=[
                "sh",
                "-c",
                "clang -O2 -target bpf "
                f"-D__TARGET_ARCH_{BPF_TARGET_ARCH.get(machine, machine)} "
                f"-I/usr/include/{machine}-linux-gnu "
                f"-DSEED={seed} -c /tmp/{object_name}.c -o /tmp/{object_name}.o && "
                f"tc qdisc replace dev {intf} clsact && "
                f"tc filter replace dev {intf} egress prio 10 "
                f"bpf da obj /tmp/{object_name}.o sec classifier",
            ],
            network_mode=f"container:{node_container.id}",
            privileged=True,
        )
        try:
            loader.put_archive("/tmp", archive.getvalue())
            loader.start()
            status = loader.wait(timeout=60).get("StatusCode", 1)
            if status:
                output = loader.logs().decode(errors="replace").strip()
                raise RuntimeCapabilityError(
                    f"could not attach switch bitflip program on {node}:{intf}: {output}"
                )
        finally:
            loader.remove(force=True)
        return token

    def attached(self, node: str, intf: str) -> bool:
        return (
            "bpf"
            in self.runtime.exec(node, f"tc filter show dev {intf} egress").lower()
        )

    def detach(self, node: str, intf: str, token: str | None = None) -> None:
        """Remove this egress classifier."""
        self.runtime.exec(
            node,
            f"tc filter del dev {intf} egress prio 10 2>/dev/null || true",
        )
