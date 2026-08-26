"""Controller-built eBPF injector for switch-internal packet corruption."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

from nika.runtime.base import RuntimeCapabilityError


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
        container = self.runtime.get_container(node)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(object_name)
            info.size = len(compiled)
            tar.addfile(info, io.BytesIO(compiled))
        container.put_archive("/tmp", archive.getvalue())
        path = f"/tmp/{object_name}"
        self.runtime.exec(
            node,
            f"tc qdisc replace dev {intf} clsact && tc filter replace dev {intf} egress prio 10 bpf da obj {path} sec classifier",
        )
        return token

    def attached(self, node: str, intf: str) -> bool:
        return (
            "bpf"
            in self.runtime.exec(node, f"tc filter show dev {intf} egress").lower()
        )

    def detach(self, node: str, intf: str, token: str | None = None) -> None:
        """Remove this egress classifier and its opaque object file."""
        path = f"/tmp/.dp-{token}.o" if token else ""
        self.runtime.exec(
            node,
            f"tc filter del dev {intf} egress prio 10 2>/dev/null || true"
            + (f"; rm -f {path}" if path else ""),
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
