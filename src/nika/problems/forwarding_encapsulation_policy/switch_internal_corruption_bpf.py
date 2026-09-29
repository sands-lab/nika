"""Controller-built eBPF injector for switch-internal packet corruption."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

from nika.net_env.utils.kathara.docker_files.docker_images import (
    ensure_nika_docker_images,
)
from nika.runtime.base import RuntimeCapabilityError

TC_BPF_IMAGE = "nika/tc-bpf"


class SwitchNamespaceBitflip:
    """Attach the failure's TC classifier without exposing build state to agents."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def attach(self, node: str, intf: str, seed: int) -> str:
        token = hashlib.blake2s(
            f"{self.runtime.lab_name}:{node}:{intf}:{seed}".encode(), digest_size=8
        ).hexdigest()
        object_name = f".dp-{token}.o"
        compiled = self._compile(seed)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(object_name)
            info.size = len(compiled)
            tar.addfile(info, io.BytesIO(compiled))
        # Lab images ship tc without libelf, so a short-lived loader container
        # joins the node's network namespace to attach the object.
        ensure_nika_docker_images([TC_BPF_IMAGE])
        node_container = self.runtime.get_container(node)
        loader = node_container.client.containers.create(
            TC_BPF_IMAGE,
            command=[
                "sh",
                "-c",
                f"tc qdisc replace dev {intf} clsact && "
                f"tc filter replace dev {intf} egress prio 10 "
                f"bpf da obj /tmp/{object_name} sec classifier",
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

    @staticmethod
    def _compile(seed: int) -> bytes:
        source = Path(__file__).with_name("switch_internal_corruption.bpf.c")
        with tempfile.TemporaryDirectory(prefix="nika-bpf-") as directory:
            obj = Path(directory) / "bitflip.o"
            result = subprocess.run(
                [
                    "clang",
                    "-O2",
                    "-target",
                    "bpf",
                    "-D__TARGET_ARCH_x86",
                    "-I/usr/include/x86_64-linux-gnu",
                    f"-DSEED={seed}",
                    "-c",
                    str(source),
                    "-o",
                    str(obj),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise RuntimeCapabilityError(
                    f"could not compile switch bitflip program: {result.stderr.strip()}"
                )
            return obj.read_bytes()
