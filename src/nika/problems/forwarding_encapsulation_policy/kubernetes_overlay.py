"""Kubernetes VXLAN overlay neighbor failure."""

from __future__ import annotations

import re
import shlex
from typing import Any, ClassVar

from pydantic import Field

from nika.problems.base import FailureDomain
from nika.problems.rca import UnresolvedRootCauseError, node_resource
from nika.problems.support.benchmark_targets import (
    choice,
    choice_distinct,
    k8s_control_node,
)
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    contains,
    shell,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

logger = system_logger

# k3s default VXLAN device (flannel backend, UDP 8472).
_OVERLAY_DEV = "flannel.1"

_MAC_RE = re.compile(r"(?i)\b(?:lladdr|link/ether)\s+([0-9a-f:]+)")
_INET_RE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)")


def _normalize_mac(value: str | None) -> str:
    return (value or "").strip().lower()


def _first_mac(output: str) -> str:
    match = _MAC_RE.search(output or "")
    return _normalize_mac(match.group(1) if match else "")


def _first_inet(output: str) -> str:
    match = _INET_RE.search(output or "")
    return match.group(1) if match else ""


def previous_vtep_mac(peer_mac: str, used: set[str]) -> str:
    """Return a stale VTEP MAC for ``peer_mac`` that no current VTEP uses.

    A recreated ``flannel.1`` gets a new locally administered MAC. The stale
    address keeps the peer's first five octets and changes the last one.
    """
    octets = [int(part, 16) for part in _normalize_mac(peer_mac).split(":")]
    if len(octets) != 6:
        raise RuntimeCapabilityError(f"Unreadable VTEP MAC {peer_mac!r}.")
    octets[0] = (octets[0] & 0xFC) | 0x02
    for step in range(1, 256):
        candidate = ":".join(
            f"{byte:02x}" for byte in (*octets[:5], (octets[5] + step) % 256)
        )
        if candidate not in used:
            return candidate
    raise RuntimeCapabilityError(f"No unused overlay MAC near {peer_mac!r}.")


class VxlanNeighborStaleParams(K8sParams):
    """Parameters for a stale VXLAN overlay neighbor on one node."""

    node_name: str = Field(
        description="Kubernetes node whose overlay neighbor for the peer VTEP is stale."
    )
    peer_name: str = Field(
        description="Remote Kubernetes node whose VTEP neighbor is wrong on node_name."
    )


class VxlanNeighborStale(K8sProblemBase):
    """The overlay neighbor for one remote VTEP still uses that VTEP's previous MAC.

    Flannel programs a permanent neighbor on ``flannel.1`` from each remote
    node's pod /24 to that node's VXLAN MAC, and a matching FDB entry that
    sends encapsulated frames to the node's underlay address (k3s default:
    VXLAN UDP 8472). Recreating ``flannel.1`` assigns a new random VTEP MAC;
    the neighbor can keep the previous mapping. See RFC 7348 and flannel's
    VXLAN backend:
    https://www.rfc-editor.org/rfc/rfc7348
    https://github.com/flannel-io/flannel#vxlan
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "isolation"
    root_cause_name: str = "k8s_vxlan_neighbor_stale"
    description = (
        "A Kubernetes node's VXLAN overlay neighbor for a remote VTEP is stale."
    )
    symptom_desc = (
        "Pod traffic from one node to the pods of one other node is lost, while "
        "the same destinations stay reachable from other nodes, other overlay "
        "neighbors on the affected node still work, and the node-to-node underlay "
        "and Kubernetes API stay healthy."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s"]
    Params = VxlanNeighborStaleParams
    RECORDED_ATTRS = (
        *K8sProblemBase.RECORDED_ATTRS,
        "overlay_dev",
        "peer_vtep",
        "original_mac",
        "injected_mac",
    )

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.overlay_dev: str = _OVERLAY_DEV
        self.peer_vtep: str | None = None
        self.original_mac: str | None = None
        self.injected_mac: str | None = None

    @classmethod
    def benchmark_inject_params(cls, ctx):
        k8s_nodes = list(ctx.roles.get("k8s_nodes") or [])
        control = k8s_control_node(ctx)
        workers = sorted(node for node in k8s_nodes if node != control)
        pool = workers or k8s_nodes
        if len(pool) >= 2:
            node_name, peer_name = choice_distinct(ctx.rng, pool, pool[0], n=2)
        else:
            node_name = choice(ctx.rng, pool, control)
            peer_name = next((node for node in k8s_nodes if node != node_name), "")
        return {
            "control_node": control,
            "node_name": node_name,
            "peer_name": peer_name,
        }

    @classmethod
    def benchmark_align_option(cls, ctx, row, field):
        node_name = row.get("node_name") or ""
        peer_name = row.get("peer_name") or ""
        if field == "node_name" and node_name and node_name == peer_name:
            k8s_nodes = list(
                getattr(ctx.net_env, "kubernetes_nodes", None)
                or ctx.roles.get("k8s_nodes")
                or []
            )
            peer = next((node for node in sorted(k8s_nodes) if node != node_name), "")
            if peer:
                return {**row, "peer_name": peer}
        return row

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        node_name = inject.get("node_name") or ""
        peer_name = inject.get("peer_name") or ""
        nodes = set(getattr(ctx.net_env, "kubernetes_nodes", None) or [])
        if node_name not in nodes:
            raise ValueError(
                f"{cls.root_cause_name} node_name must be a Kubernetes node, "
                f"got {node_name!r}"
            )
        if peer_name not in nodes:
            raise ValueError(
                f"{cls.root_cause_name} peer_name must be a Kubernetes node, "
                f"got {peer_name!r}"
            )
        if node_name == peer_name:
            raise ValueError(
                f"{cls.root_cause_name} node_name and peer_name must be distinct"
            )

    def root_cause_resources(self, params: VxlanNeighborStaleParams):
        node = (params.node_name or "").strip()
        if not node:
            raise UnresolvedRootCauseError(
                "k8s_vxlan_neighbor_stale needs node_name for a unique resource."
            )
        return [node_resource(node)]

    def oracle_playbook(self, params: VxlanNeighborStaleParams) -> OraclePlaybook:
        def neighbor_args(outputs):
            observed = outputs["peer_vtep"]
            vtep, mac = _first_inet(observed), _first_mac(observed)
            if not vtep or not mac:
                raise ValueError("The peer VTEP address and MAC were not observed.")
            return {
                "host_name": params.node_name,
                "command": f"echo peer_vtep={vtep} peer_mac={mac}; "
                f"ip neigh show dev {_OVERLAY_DEV} to {vtep}; "
                f"echo FDB; ip -f bridge neigh show dev {_OVERLAY_DEV}; "
                f"ip route show dev {_OVERLAY_DEV}",
                "timeout": 15,
            }

        def stale_neighbor(output):
            match = re.search(r"peer_vtep=([0-9.]+) peer_mac=([0-9a-f:]+)", output)
            if not match or "FDB\n" not in output:
                return False
            neighbor, fdb = output.split("FDB\n", 1)
            vtep, mac = match.groups()
            observed = re.search(
                rf"^{re.escape(vtep)}\s+(?:dev {re.escape(_OVERLAY_DEV)}\s+)?"
                r"lladdr ([0-9a-f:]+)\s+PERMANENT\b",
                neighbor,
                re.M | re.I,
            )
            return bool(
                observed
                and _normalize_mac(observed[1]) != mac
                and re.search(
                    rf"^[0-9.]+\s+lladdr {re.escape(mac)}\s+PERMANENT\b",
                    fdb,
                    re.M | re.I,
                )
                and re.search(rf"\bvia {re.escape(vtep)}\b", fdb)
            )

        return OraclePlaybook(
            finding=f"The permanent VXLAN neighbor on {params.node_name} uses a different MAC from {params.peer_name}'s live VTEP, while the FDB still maps the live MAC.",
            steps=[
                shell(
                    params.peer_name,
                    f"ip -d link show dev {_OVERLAY_DEV}; ip -4 addr show dev {_OVERLAY_DEV}",
                    "Read the peer's live VXLAN interface address and MAC.",
                    contains(r"vxlan", "The peer has a VXLAN interface."),
                    save_as="peer_vtep",
                ),
                OracleStep(
                    "Compare the local permanent neighbor with the peer's observed MAC, FDB, and overlay route.",
                    "exec_shell",
                    neighbor_args,
                    Check(
                        "The routed peer VTEP has a wrong permanent neighbor MAC, while its live MAC remains in the FDB, distinguishing stale neighbor from FDB loss.",
                        stale_neighbor,
                    ),
                    evidence=True,
                    attempts=3,
                ),
            ],
        )

    def _require_overlay(self, node: str) -> str:
        output = self.runtime.exec(
            node, f"ip -d link show dev {shlex.quote(_OVERLAY_DEV)} 2>/dev/null || true"
        )
        if "vxlan" not in output.lower() and "flannel" not in output.lower():
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: no VXLAN overlay device "
                f"{_OVERLAY_DEV!r} on {node!r}."
            )
        self.overlay_dev = _OVERLAY_DEV
        return _OVERLAY_DEV

    def _peer_vtep(self, params: VxlanNeighborStaleParams) -> tuple[str, str]:
        dev = _OVERLAY_DEV
        inet = _first_inet(
            self.runtime.exec(
                params.peer_name,
                f"ip -4 addr show dev {shlex.quote(dev)} 2>/dev/null || true",
            )
        )
        mac = _first_mac(
            self.runtime.exec(
                params.peer_name,
                f"ip -o link show dev {shlex.quote(dev)} 2>/dev/null || true",
            )
        )
        if not inet or not mac:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: peer {params.peer_name!r} has no "
                f"overlay address on {dev}."
            )
        return inet, mac

    def _neigh_mac(self, node: str, vtep: str) -> str:
        return _first_mac(
            self.runtime.exec(
                node,
                f"ip neigh show dev {shlex.quote(_OVERLAY_DEV)} "
                f"to {shlex.quote(vtep)} 2>/dev/null || true",
            )
        )

    def _set_neigh(self, node: str, vtep: str, mac: str) -> None:
        self.runtime.exec(
            node,
            f"ip neigh replace {shlex.quote(vtep)} lladdr {shlex.quote(mac)} "
            f"nud permanent dev {shlex.quote(_OVERLAY_DEV)}",
        )

    def _overlay_macs(self) -> set[str]:
        used: set[str] = set()
        for node in self.cluster_nodes():
            mac = _first_mac(
                self.runtime.exec(
                    node,
                    f"ip -o link show dev {shlex.quote(_OVERLAY_DEV)} "
                    "2>/dev/null || true",
                )
            )
            if mac:
                used.add(mac)
        return used

    def inject_fault(self, params: VxlanNeighborStaleParams) -> None:
        node = params.node_name
        if node == params.peer_name or not {node, params.peer_name} <= set(
            self.cluster_nodes()
        ):
            raise ValueError(
                "VXLAN neighbor targets must be distinct Kubernetes nodes."
            )
        self._require_overlay(node)
        vtep, peer_mac = self._peer_vtep(params)
        current = self._neigh_mac(node, vtep)
        if current != peer_mac:
            raise RuntimeCapabilityError(
                f"Overlay neighbor for {params.peer_name!r} is missing or already stale."
            )
        injected = previous_vtep_mac(peer_mac, self._overlay_macs())
        self.peer_vtep = vtep
        self.original_mac = current
        self.injected_mac = injected
        self._set_neigh(node, vtep, injected)
        logger.info(
            f"Set overlay neighbor {vtep} on {node} toward {params.peer_name} "
            f"to {injected} (was {current})."
        )

    def verify_fault(self, params: VxlanNeighborStaleParams) -> dict:
        node = params.node_name
        vtep = self.peer_vtep or self._peer_vtep(params)[0]
        expected = _normalize_mac(self.injected_mac)

        def evaluate() -> tuple[bool, dict[str, Any]]:
            observed = self._neigh_mac(node, vtep)
            return bool(expected) and observed == expected, {
                "node": node,
                "peer": params.peer_name,
                "overlay_dev": _OVERLAY_DEV,
                "peer_vtep": vtep,
                "neighbor_mac": observed,
                "expected_mac": expected,
            }

        return self.poll_verify(evaluate)

    def recheck_artifact(self, params: VxlanNeighborStaleParams) -> dict:
        vtep = self.peer_vtep or self._peer_vtep(params)[0]
        observed = self._neigh_mac(params.node_name, vtep)
        expected = _normalize_mac(self.injected_mac)
        present = bool(expected) and observed == expected
        return {
            "present": present,
            "fault": self.root_cause_name,
            "scope": "artifact",
            "evidence": {
                "node": params.node_name,
                "peer_vtep": vtep,
                "neighbor_mac": observed,
            },
            "error": None if present else "fault artifact absent",
        }

    def recover_fault(self, params: VxlanNeighborStaleParams) -> dict:
        vtep, peer_mac = self._peer_vtep(params)
        original = peer_mac
        injected = _normalize_mac(self.injected_mac)
        if original:
            self._set_neigh(params.node_name, vtep, original)
        observed = self._neigh_mac(params.node_name, vtep)
        restored = bool(original) and observed == original and observed != injected
        return {
            "verified": restored,
            "details": {
                "node": params.node_name,
                "peer_vtep": vtep,
                "neighbor_mac": observed,
            },
        }
