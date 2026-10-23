import ipaddress
import json
import re

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

_NETWORK_RE = re.compile(r"^\s*network\s+(\S+)\s+area\s+(\S+)\s*$", re.MULTILINE)


def _ospf_block(config: str) -> str:
    """The ``router ospf`` block of an FRR config (indented member lines)."""
    match = re.search(r"^router ospf\b.*?(?=^\S)", config + "\n!", re.M | re.S)
    return match.group(0) if match else ""


def ospf_networks(config: str) -> list[tuple[str, str]]:
    """``(prefix, area)`` of each ``network`` statement in the ``router ospf`` block."""
    return _NETWORK_RE.findall(_ospf_block(config))


_INTERFACE_RE = re.compile(r"^interface\s+(\S+)\s*$")
_INTERFACE_AREA_RE = re.compile(r"^\s+ip ospf area\s+(\S+)\s*$")
_COMMENTED_INTERFACE_AREA_RE = re.compile(r"^\s+!\s+ip ospf area\s+(\S+)\s*$")


def _ospf_interface_areas(
    config: str, *, commented: bool = False
) -> list[tuple[str, str]]:
    """Extract active or commented ``(interface, area)`` bindings from FRR config."""
    pattern = _COMMENTED_INTERFACE_AREA_RE if commented else _INTERFACE_AREA_RE
    bindings = []
    interface = None
    for line in config.splitlines():
        if match := _INTERFACE_RE.fullmatch(line):
            interface = match[1]
        elif line and not line[0].isspace() and not line.startswith("!"):
            interface = None
        elif interface and (match := pattern.fullmatch(line)):
            bindings.append((interface, match[1]))
    return bindings


def _ospf_area_bindings(
    config: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Extract one supported OSPF binding style, rejecting mixed active syntax."""
    networks = ospf_networks(config)
    interfaces = _ospf_interface_areas(config)
    if networks and interfaces:
        raise RuntimeCapabilityError(
            "OSPF configuration mixes network statements and interface area bindings"
        )
    return networks, interfaces


# ==================================================================
# Problem: OSPF Area Misconfiguration
# ==================================================================


class OSPFAreaMisconfigParams(BaseModel):
    """Parameters for injecting an OSPF area misconfiguration fault."""

    host_name: str = Field(description="Target router host name.")


class OSPFAreaMisconfig(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "ospf_area_misconfiguration"
    description = "OSPF area is misconfigured between neighbors."
    effect_protocol = "ospf"

    TAGS: str = ["ospf"]

    Params = OSPFAreaMisconfigParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFAreaMisconfigParams):
        return [node_resource(params.host_name)]

    def _inject_native(self, params: OSPFAreaMisconfigParams, ops) -> None:
        # Move the router's first area to a wrong area ID, the native form of
        # rewriting FRR ``network ... area`` statements.
        areas = ops.ospf_interface_areas()
        if not areas:
            raise RuntimeError(f"Could not find OSPF area on {params.host_name}")
        self._orig_areas = set(areas)
        correct_area = areas[0]
        wrong_area = "0.0.0.66" if correct_area != "0.0.0.66" else "0.0.0.99"
        ops.renumber_ospf_area(correct_area, wrong_area)
        self.logger.info(
            f"Injected OSPF area misconfiguration on {params.host_name} from "
            f"area {correct_area} to {wrong_area}."
        )

    def inject_fault(self, params: OSPFAreaMisconfigParams):
        ops = native_fault_ops(self, params.host_name, "renumber_ospf_area")
        if ops is not None:
            self._inject_native(params, ops)
            return
        running_cfg = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config'"
        )
        statements, interface_bindings = _ospf_area_bindings(running_cfg)
        if not statements and not interface_bindings:
            raise RuntimeError(f"Could not find OSPF area on {params.host_name}")
        correct_area = (statements or interface_bindings)[0][-1]
        wrong_area = "66" if correct_area != "66" else "99"
        # Keep the original area set in memory; a .bak file would expose it.
        self._orig_areas = {binding[-1] for binding in statements + interface_bindings}
        if statements:
            # Move the statements in the running daemon (no restart), then keep
            # the startup file consistent with it.
            moves = " ".join(
                f"-c 'no network {prefix} area {area}' -c 'network {prefix} area {wrong_area}'"
                for prefix, area in statements
                if area == correct_area
            )
            self.runtime.exec(
                params.host_name,
                f"vtysh -c 'configure terminal' -c 'router ospf' {moves} -c 'end'",
            )
            self.runtime.exec(
                params.host_name,
                f"sed -i -E 's/(area ){correct_area}$/\\1{wrong_area}/g' /etc/frr/frr.conf",
            )
        else:
            moves = " ".join(
                f"-c 'interface {interface}' -c 'no ip ospf area {area}' -c 'ip ospf area {wrong_area}'"
                for interface, area in interface_bindings
                if area == correct_area
            )
            self.runtime.exec(
                params.host_name,
                f"vtysh -c 'configure terminal' {moves} -c 'end'",
            )
            self.runtime.exec(
                params.host_name,
                "sed -i -E "
                f"'s/^([[:space:]]*ip ospf area ){re.escape(correct_area)}$/\\1{wrong_area}/' "
                "/etc/frr/frr.conf",
            )
        self.logger.info(
            f"Injected OSPF area misconfiguration on {params.host_name} from area {correct_area} to {wrong_area}."
        )

    def verify_fault(self, params: OSPFAreaMisconfigParams) -> dict:
        """Verify the OSPF area in frr.conf and in the running daemon was changed."""
        ops = native_fault_ops(self, params.host_name, "ospf_interface_areas")
        if ops is not None:
            orig_areas = set(getattr(self, "_orig_areas", None) or ())
            running_areas = set(ops.ospf_interface_areas())
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(running_areas) and running_areas != orig_areas,
                details={
                    "host": params.host_name,
                    "orig_areas": sorted(orig_areas),
                    "running_areas": sorted(running_areas),
                },
            )
        file_config = self.runtime.exec(params.host_name, "cat /etc/frr/frr.conf")
        running_config = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
        )
        file_networks, file_interfaces = _ospf_area_bindings(file_config)
        running_networks, running_interfaces = _ospf_area_bindings(running_config)
        file_areas = {binding[-1] for binding in file_networks + file_interfaces}
        orig_areas = set(getattr(self, "_orig_areas", None) or ())
        running_areas = {
            binding[-1] for binding in running_networks + running_interfaces
        }
        file_changed = (
            bool(file_areas) and bool(orig_areas) and file_areas != orig_areas
        )
        daemon_changed = bool(running_areas) and running_areas != orig_areas
        verified = file_changed and daemon_changed
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "file_areas": list(file_areas),
                "orig_areas": list(orig_areas),
                "running_areas": list(running_areas),
            },
        )


# ==================================================================
# Problem: OSPF Neighbor Missing
# ==================================================================


# sed address range for the ``router ospf`` block: FRR indents block members, so
# the block ends at the next unindented line (``!``, ``exit``, next ``router``).
_OSPF_BLOCK_RANGE = "/^router ospf/,/^[^[:space:]]/"


class OSPFNeighborMissingParams(BaseModel):
    """Parameters for injecting an OSPF neighbor missing fault."""

    host_name: str = Field(description="Target router host name.")


class OSPFNeighborMissing(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "ospf_neighbor_missing"
    description = "Expected OSPF neighbor adjacency is missing."
    effect_protocol = "ospf"

    TAGS: str = ["ospf"]

    Params = OSPFNeighborMissingParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFNeighborMissingParams):
        return [node_resource(params.host_name)]

    def _single_adjacency_binding(self, host: str) -> tuple[str, str, str]:
        """Select one network or interface binding covering exactly one Full peer."""
        config = self.runtime.exec(host, "vtysh -c 'show running-config'")
        statements, interfaces = _ospf_area_bindings(config)
        raw = self.runtime.exec(host, "vtysh -c 'show ip ospf neighbor json'")
        try:
            neighbors = json.loads(raw).get("neighbors") or {}
        except (ValueError, AttributeError):
            neighbors = {}
        full_peers = []
        for entries in neighbors.values():
            for entry in entries if isinstance(entries, list) else [entries]:
                if not isinstance(entry, dict) or not str(
                    entry.get("nbrState", "")
                ).startswith("Full"):
                    continue
                iface_name = str(entry.get("ifaceName", ""))
                if ":" not in iface_name:
                    continue
                interface, address = iface_name.split(":", 1)
                try:
                    full_peers.append((interface, ipaddress.ip_address(address)))
                except ValueError:
                    continue
        local_ips = [address for _, address in full_peers]
        for prefix, area in statements:
            network = ipaddress.ip_network(prefix, strict=False)
            if sum(ip in network for ip in local_ips) == 1:
                return "network", prefix, area
        for interface, area in interfaces:
            if (
                sum(peer_interface == interface for peer_interface, _ in full_peers)
                == 1
            ):
                return "interface", interface, area
        raise RuntimeCapabilityError(
            f"{type(self).__name__}: no OSPF binding on {host!r} covers exactly one Full adjacency."
        )

    def inject_fault(self, params: OSPFNeighborMissingParams):
        # Drop the one binding that brings a single adjacency into OSPF.
        kind, binding, area = self._single_adjacency_binding(params.host_name)
        if kind == "network":
            prefix = binding
            self.runtime.exec(
                params.host_name,
                "vtysh -c 'configure terminal' -c 'router ospf' "
                f"-c 'no network {prefix} area {area}' -c 'end'",
            )
            line = f"network {prefix} area {area}".replace(".", "\\.").replace(
                "/", "\\/"
            )
            self.runtime.exec(
                params.host_name,
                "sed -i -E "
                f"'{_OSPF_BLOCK_RANGE} s/^([[:space:]]+)({line})[[:space:]]*$/\\1# \\2/' "
                "/etc/frr/frr.conf",
            )
        else:
            interface = binding
            self.runtime.exec(
                params.host_name,
                "vtysh -c 'configure terminal' "
                f"-c 'interface {interface}' -c 'no ip ospf area {area}' -c 'end'",
            )
            interface_pattern = re.escape(interface)
            self.runtime.exec(
                params.host_name,
                "sed -i -E "
                f"'/^interface {interface_pattern}$/,/^[^[:space:]!#]/ "
                f"s/^([[:space:]]*)ip ospf area {re.escape(area)}$/\\1! ip ospf area {area}/' "
                "/etc/frr/frr.conf",
            )
        self.logger.info(
            f"Injected OSPF neighbor missing on {params.host_name} "
            f"(removed {kind} binding {binding} area {area})."
        )

    def verify_fault(self, params: OSPFNeighborMissingParams) -> dict:
        """Verify one OSPF binding is gone while the other bindings remain."""
        file_config = self.runtime.exec(params.host_name, "cat /etc/frr/frr.conf")
        running_config = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
        )
        commented = re.findall(
            r"^\s*#\s*network\s+(\S+)\s+area\s+(\S+)",
            self.runtime.exec(
                params.host_name,
                f"sed -n -E '{_OSPF_BLOCK_RANGE} p' /etc/frr/frr.conf 2>/dev/null",
            ),
            re.MULTILINE,
        )
        dropped_interfaces = _ospf_interface_areas(file_config, commented=True)
        file_networks, file_interfaces = _ospf_area_bindings(file_config)
        running_networks, running_interfaces = _ospf_area_bindings(running_config)
        dropped = [tuple(item) for item in commented]
        if dropped_interfaces:
            verified = (
                len(dropped_interfaces) == 1
                and not dropped
                and set(file_interfaces) == set(running_interfaces)
                and dropped_interfaces[0] not in running_interfaces
                and bool(running_interfaces)
                and not file_networks
                and not running_networks
            )
            dropped_interface_binding = list(dropped_interfaces[0])
        else:
            verified = (
                len(dropped) == 1
                and dropped[0] not in running_networks
                and len(running_networks) > 0
                and set(file_networks) == set(running_networks)
                and not file_interfaces
                and not running_interfaces
            )
            dropped_interface_binding = None
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "dropped_network": list(dropped[0]) if dropped else None,
                "dropped_interface_binding": dropped_interface_binding,
                "running_network_count": len(running_networks),
                "running_interface_binding_count": len(running_interfaces),
            },
        )
