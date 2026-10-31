"""Controller-built eBPF injector for switch-internal packet corruption."""

from __future__ import annotations

import io
import platform
import tarfile
from pathlib import Path

from nika.net_env.utils.kathara.docker_files.docker_images import (
    ensure_nika_docker_images,
    nika_image,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.shared.containers import docker_client

TC_BPF_IMAGE = nika_image("tc-bpf")
# platform.machine() -> libbpf __TARGET_ARCH_* suffix.
BPF_TARGET_ARCH = {"x86_64": "x86", "aarch64": "arm64"}
# ``tc filter show`` prints the object file name.
_OBJECT = "tc_egress"
_PRIO = 10


class SwitchNamespaceBitflip:
    """Attach the failure's TC classifier without exposing build state to agents.

    On Kathara the classifier sits on the device's own interface, in the node's
    network namespace. On Containerlab it sits on the host-side wire end that
    carries what the device sends out of that interface (see
    ``nika.runtime.containerlab.wires``), outside every node, so it works the
    same whatever datapath the router runs.
    """

    def __init__(self, runtime, backend: str) -> None:
        self.runtime = runtime
        self._host_wires = backend == "containerlab"

    def _wire(self):
        from nika.service.containerlab.host_tc import HostTcController

        return HostTcController(self.runtime)

    def attach(self, node: str, intf: str, seed: int) -> str:
        """Attach the classifier; return the device it is attached to."""
        if self._host_wires:
            dev = self._wire().egress_dev(node, intf)
            # Wire ends already carry a clsact qdisc for their redirect.
            self._load(dev, seed, network_mode="host", qdisc=False)
            return dev
        node_container = self.runtime.get_container(node)
        self._load(
            intf, seed, network_mode=f"container:{node_container.id}", qdisc=True
        )
        return intf

    def _load(self, dev: str, seed: int, *, network_mode: str, qdisc: bool) -> None:
        source = Path(__file__).with_name("switch_internal_corruption.bpf.c")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(source, arcname=f"{_OBJECT}.c")
        # nika/tc-bpf is built for the host architecture.
        machine = platform.machine()
        # Lab images ship tc without libelf or clang, so a short-lived loader
        # container builds the object and joins the target network namespace
        # to attach it.
        ensure_nika_docker_images([TC_BPF_IMAGE])
        steps = [
            "clang -O2 -target bpf "
            f"-D__TARGET_ARCH_{BPF_TARGET_ARCH.get(machine, machine)} "
            f"-I/usr/include/{machine}-linux-gnu "
            f"-DSEED={seed} -c /tmp/{_OBJECT}.c -o /tmp/{_OBJECT}.o",
            *([f"tc qdisc replace dev {dev} clsact"] if qdisc else []),
            f"tc filter replace dev {dev} egress prio {_PRIO} "
            f"bpf da obj /tmp/{_OBJECT}.o sec classifier",
        ]
        loader = docker_client().containers.create(
            TC_BPF_IMAGE,
            command=["sh", "-c", " && ".join(steps)],
            network_mode=network_mode,
            privileged=True,
        )
        try:
            loader.put_archive("/tmp", archive.getvalue())
            loader.start()
            status = loader.wait(timeout=60).get("StatusCode", 1)
            if status:
                output = loader.logs().decode(errors="replace").strip()
                raise RuntimeCapabilityError(
                    f"could not attach switch bitflip program on {dev}: {output}"
                )
        finally:
            loader.remove(force=True)

    def _filters(self, node: str, intf: str) -> str:
        if self._host_wires:
            wire = self._wire()
            return wire.egress_filters(wire.egress_dev(node, intf))
        return self.runtime.exec(node, f"tc filter show dev {intf} egress")

    def attached(self, node: str, intf: str) -> bool:
        return "bpf" in self._filters(node, intf).lower()

    def detach(self, node: str, intf: str) -> None:
        """Remove this egress classifier."""
        if self._host_wires:
            wire = self._wire()
            dev = wire.egress_dev(node, intf)
            if "bpf" in wire.egress_filters(dev):
                wire.delete_egress_filter(dev, _PRIO)
            return
        self.runtime.exec(
            node,
            f"tc filter del dev {intf} egress prio {_PRIO} 2>/dev/null || true",
        )
