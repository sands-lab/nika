"""Controller-built eBPF injector for switch-internal packet corruption."""

from __future__ import annotations

import io
import json
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


class SwitchEgressClassifier:
    """Attach the failure's TC classifier without exposing build state to agents.

    On Kathara the classifier sits on the VDE proxy port toward the peer.
    On Containerlab it sits on the host-side wire end that
    carries what the device sends out of that interface (see
    ``nika.runtime.containerlab.wires``), outside every node, so it works the
    same whatever datapath the router runs.
    """

    SOURCE = ""

    def __init__(self, runtime, backend: str = "kathara") -> None:
        self.runtime = runtime
        self._host_wires = backend == "containerlab"

    def _wire(self):
        from nika.service.containerlab.host_tc import HostTcController

        return HostTcController(self.runtime)

    def _proxy(self):
        from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy

        return KatharaVdeFaultProxy(self.runtime)

    def attach(
        self,
        node: str,
        intf: str,
        seed: int,
        local_udp_ports: set[int] = frozenset(),
    ) -> None:
        """Attach the classifier to the selected egress."""
        if self._host_wires:
            dev = self._wire().egress_dev(node, intf)
            # Wire ends already carry a clsact qdisc for their redirect.
            self._load(
                dev,
                seed,
                local_udp_ports,
                network_mode="host",
                qdisc=False,
            )
            return
        proxy = self._proxy()
        state = proxy.insert(node, intf, allow_shared=True)
        try:
            self._load(
                "eth1",
                seed,
                local_udp_ports,
                network_mode=f"container:{state.proxy_id}",
                qdisc=True,
            )
        except BaseException:
            proxy.remove(state)
            raise

    def _load(
        self,
        dev: str,
        seed: int,
        local_udp_ports: set[int],
        *,
        network_mode: str,
        qdisc: bool,
    ) -> None:
        source = Path(__file__).with_name(self.SOURCE)
        archive = io.BytesIO()
        ports = (
            " || ".join(f"(port) == {port}u" for port in sorted(local_udp_ports)) or "0"
        )
        content = (
            f"#define LOCAL_UDP_PORT(port) ({ports})\n".encode() + source.read_bytes()
        )
        with tarfile.open(fileobj=archive, mode="w") as tar:
            entry = tarfile.TarInfo(f"{_OBJECT}.c")
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
        # nika/tc-bpf is built for the host architecture.
        machine = platform.machine()
        # Lab images ship tc without libelf or clang, so a short-lived loader
        # container builds the object and joins the external proxy or host
        # network namespace to attach it.
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
            labels={"nika.lab_name": self.runtime.lab_name},
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
            return wire._run(
                "tc",
                "-j",
                "filter",
                "show",
                "dev",
                wire.egress_dev(node, intf),
                "egress",
            )
        state = self._proxy().discover(node, intf)
        if state is None:
            return "[]"
        result = (
            docker_client()
            .containers.get(state.proxy_id)
            .exec_run(["tc", "-j", "filter", "show", "dev", "eth1", "egress"])
        )
        if result.exit_code:
            raise RuntimeCapabilityError("Cannot read VDE proxy egress filters")
        return result.output.decode()

    def attached(self, node: str, intf: str) -> bool:
        output = self._filters(node, intf)
        try:
            filters = json.loads(output)
        except ValueError as exc:
            raise RuntimeCapabilityError(
                f"Cannot read egress filters on {node}:{intf}: {output!r}"
            ) from exc
        if not isinstance(filters, list):
            raise RuntimeCapabilityError(
                f"Invalid egress filters on {node}:{intf}: {output!r}"
            )
        return any(
            item.get("kind") == "bpf" and item.get("pref") == _PRIO for item in filters
        )

    def detach(self, node: str, intf: str) -> None:
        """Remove this egress classifier."""
        if self._host_wires:
            wire = self._wire()
            dev = wire.egress_dev(node, intf)
            if "bpf" in wire.egress_filters(dev):
                wire.delete_egress_filter(dev, _PRIO)
            return
        proxy = self._proxy()
        state = proxy.discover(node, intf)
        if state is not None:
            proxy.remove(state)


class SwitchNamespaceBitflip(SwitchEgressClassifier):
    SOURCE = "switch_internal_corruption.bpf.c"
