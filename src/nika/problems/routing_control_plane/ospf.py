import ipaddress
import json
import re

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.support.frr import FRR_CONF, frr_config_command
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


def _ospf_interface_areas(config: str) -> list[tuple[str, str]]:
    """Extract ``(interface, area)`` bindings from FRR config."""
    bindings = []
    interface = None
    for line in config.splitlines():
        if match := _INTERFACE_RE.fullmatch(line):
            interface = match[1]
        elif line and not line[0].isspace() and not line.startswith("!"):
            interface = None
        elif interface and (match := _INTERFACE_AREA_RE.fullmatch(line)):
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
    native_router_operations = frozenset({"ospf_interface_areas", "renumber_ospf_area"})

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
        # Move the bindings in the running daemon (no restart), then keep the
        # startup file consistent with it.
        if statements:
            moves = [
                line
                for prefix, area in statements
                if area == correct_area
                for line in (
                    f"no network {prefix} area {area}",
                    f"network {prefix} area {wrong_area}",
                )
            ]
            self.runtime.exec(
                params.host_name, frr_config_command("router ospf", *moves)
            )
            self.runtime.exec(
                params.host_name,
                f"sed -i -E 's/(area ){correct_area}$/\\1{wrong_area}/g' {FRR_CONF}",
            )
        else:
            moves = [
                line
                for interface, area in interface_bindings
                if area == correct_area
                for line in (
                    f"interface {interface}",
                    f"no ip ospf area {area}",
                    f"ip ospf area {wrong_area}",
                )
            ]
            self.runtime.exec(params.host_name, frr_config_command(*moves))
            self.runtime.exec(
                params.host_name,
                "sed -i -E "
                f"'s/^([[:space:]]*ip ospf area ){re.escape(correct_area)}$/\\1{wrong_area}/' "
                f"{FRR_CONF}",
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


def _single_adjacency_interface(host: str, neighbor_interfaces: list[str]) -> str:
    """First interface, in name order, that carries exactly one Full adjacency."""
    for interface in sorted(set(neighbor_interfaces)):
        if neighbor_interfaces.count(interface) == 1:
            return interface
    raise RuntimeCapabilityError(
        f"OSPFNeighborMissing: no OSPF interface on {host!r} carries exactly one "
        "Full adjacency."
    )


class OSPFNeighborMissing(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "ospf_neighbor_missing"
    description = "Expected OSPF neighbor adjacency is missing."
    effect_protocol = "ospf"

    TAGS: str = ["ospf"]
    native_router_operations = frozenset(
        {"ospf_interfaces", "ospf_neighbor_interfaces", "remove_ospf_interface"}
    )
    supported_backends = ("kathara", "containerlab")

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

    def _platform_interface_ops(self, host: str):
        """``(interfaces, neighbor_interfaces, remove)`` of a non-FRR router.

        Returns ``None`` for FRR routers, which keep the ``frr.conf`` path.
        """
        ops = native_fault_ops(self, host, "remove_ospf_interface")
        if ops is not None:
            return (
                lambda: ops.ospf_interfaces(),
                lambda: ops.ospf_neighbor_interfaces(),
                lambda interface: ops.remove_ospf_interface(interface),
            )
        if self.lab_backend == "containerlab":
            runtime = self.runtime
            return (
                lambda: runtime.srl_ospf_interfaces(host),
                lambda: runtime.srl_ospf_neighbor_interfaces(host),
                lambda interface: runtime.srl_remove_ospf_interface(host, interface),
            )
        return None

    def inject_fault(self, params: OSPFNeighborMissingParams):
        platform = self._platform_interface_ops(params.host_name)
        if platform is not None:
            # Remove the OSPF interface of one adjacency, the native form of
            # deleting the ``network`` statement that covers it.
            interfaces, neighbor_interfaces, remove = platform
            interface = _single_adjacency_interface(
                params.host_name, neighbor_interfaces()
            )
            self._orig_interfaces = interfaces()
            remove(interface)
            self._dropped_binding = ("interface", interface, None)
            self.logger.info(
                f"Injected OSPF neighbor missing on {params.host_name} "
                f"(removed OSPF interface {interface})."
            )
            return
        # Drop the one binding that brings a single adjacency into OSPF, the
        # way a deleted ``network`` line loses that neighbor and only it. The
        # startup file loses the line too, leaving no commented-out line that
        # points at the change.
        kind, binding, area = self._single_adjacency_binding(params.host_name)
        if kind == "network":
            prefix = binding
            self.runtime.exec(
                params.host_name,
                frr_config_command("router ospf", f"no network {prefix} area {area}"),
            )
            line = f"network {prefix} area {area}".replace(".", "\\.").replace(
                "/", "\\/"
            )
            self.runtime.exec(
                params.host_name,
                "sed -i -E "
                f"'{_OSPF_BLOCK_RANGE} {{/^[[:space:]]+{line}[[:space:]]*$/d}}' "
                f"{FRR_CONF}",
            )
        else:
            interface = binding
            self.runtime.exec(
                params.host_name,
                frr_config_command(f"interface {interface}", f"no ip ospf area {area}"),
            )
            interface_pattern = re.escape(interface)
            self.runtime.exec(
                params.host_name,
                "sed -i -E "
                f"'/^interface {interface_pattern}$/,/^[^[:space:]!#]/ "
                f"{{/^[[:space:]]*ip ospf area {re.escape(area)}$/d}}' "
                f"{FRR_CONF}",
            )
        self._dropped_binding = (kind, binding, area)
        self.logger.info(
            f"Injected OSPF neighbor missing on {params.host_name} "
            f"(removed {kind} binding {binding} area {area})."
        )

    def verify_fault(self, params: OSPFNeighborMissingParams) -> dict:
        """Verify one OSPF binding is gone while the other bindings remain."""
        kind, binding, area = getattr(self, "_dropped_binding", None) or (
            None,
            None,
            None,
        )
        platform = self._platform_interface_ops(params.host_name)
        if platform is not None:
            orig = set(getattr(self, "_orig_interfaces", None) or ())
            running = set(platform[0]())
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(running)
                and binding in orig
                and running == orig - {binding},
                details={
                    "host": params.host_name,
                    "removed_interface": binding,
                    "running_interface_count": len(running),
                },
            )
        file_config = self.runtime.exec(params.host_name, f"cat {FRR_CONF}")
        running_config = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
        )
        file_networks, file_interfaces = _ospf_area_bindings(file_config)
        running_networks, running_interfaces = _ospf_area_bindings(running_config)
        if kind == "interface":
            verified = (
                (binding, area) not in running_interfaces
                and (binding, area) not in file_interfaces
                and set(file_interfaces) == set(running_interfaces)
                and bool(running_interfaces)
                and not file_networks
                and not running_networks
            )
        else:
            verified = (
                kind == "network"
                and (binding, area) not in running_networks
                and (binding, area) not in file_networks
                and set(file_networks) == set(running_networks)
                and bool(running_networks)
                and not file_interfaces
                and not running_interfaces
            )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "dropped_network": [binding, area] if kind == "network" else None,
                "dropped_interface_binding": (
                    [binding, area] if kind == "interface" else None
                ),
                "running_network_count": len(running_networks),
                "running_interface_binding_count": len(running_interfaces),
            },
        )
