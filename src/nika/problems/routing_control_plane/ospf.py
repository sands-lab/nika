import ipaddress
import json
import re
import time

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.support.frr import FRR_CONF, frr_config_command, frr_save_config
from nika.problems.support.polling import wait_until
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
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


def _full_frr_neighbors(raw: str) -> set[tuple[str, str]]:
    """Full adjacency identities, independent of transient timers and DR role."""
    neighbors = json.loads(raw).get("neighbors")
    if not isinstance(neighbors, dict):
        raise ValueError("Unreadable OSPF neighbors")
    return {
        (router_id, str(entry.get("ifaceName", "")))
        for router_id, entries in neighbors.items()
        for entry in (entries if isinstance(entries, list) else [entries])
        if isinstance(entry, dict) and str(entry.get("nbrState", "")).startswith("Full")
    }


def _ospf_health(problem, host: str) -> dict[str, set]:
    """Read Full neighbors and installed OSPF routes for recovery checks."""
    from nika.validation.symptom.routing_probes import _ospf_routes

    ops = native_fault_ops(problem, host, "ospf_neighbors")
    neighbors = (
        {
            (neighbor.router_id, neighbor.interface)
            for neighbor in ops.ospf_neighbors()
            if neighbor.state.lower().startswith("full")
        }
        if ops is not None
        else _full_frr_neighbors(
            problem.runtime.exec(host, "vtysh -c 'show ip ospf neighbor json'")
        )
    )
    return {"neighbors": neighbors, "routes": _ospf_routes(problem, host)}


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


class OSPFNeighborMissing(ProblemBase):
    """One OSPF interface loses its protocol binding and stops sending Hellos.

    RFC 2328 section 9.2 describes interface activation; section 10.3 expires
    the neighbor after its inactivity timer. Remove a binding covering exactly
    one Full adjacency, preserving the other bindings and node ground truth.
    https://www.rfc-editor.org/rfc/rfc2328#section-9.2
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "ospf_neighbor_missing"
    description = "Expected OSPF neighbor adjacency is missing."
    effect_protocol = "ospf"

    TAGS: str = ["ospf"]
    native_router_operations = frozenset(
        {
            "ospf_neighbors",
            "ospf_bindings",
            "remove_ospf_binding",
            "ipv4_addresses",
            "ospf_routes",
            "save_config",
            "restore_config",
        }
    )

    Params = OSPFNeighborMissingParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "ospf"
        )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFNeighborMissingParams):
        return [node_resource(params.host_name)]

    def _route_prefixes(self, host: str) -> set[str]:
        ops = native_fault_ops(self, host, "ospf_routes")
        if ops is not None:
            return ops.ospf_routes()
        return set(
            json.loads(self.runtime.exec(host, "vtysh -c 'show ip route ospf json'"))
        )

    def _single_adjacency_binding(self, host: str) -> tuple[str, str, str]:
        """Select one network or interface binding covering exactly one Full peer."""
        config = self.runtime.exec(host, "vtysh -c 'show running-config'")
        statements, interfaces = _ospf_area_bindings(config)
        raw = self.runtime.exec(host, "vtysh -c 'show ip ospf neighbor json'")
        self._frr_neighbors = _full_frr_neighbors(raw)
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
        self._original_routes = self._route_prefixes(params.host_name)
        ops = native_fault_ops(self, params.host_name, "remove_ospf_binding")
        if ops is not None:
            neighbors = [
                n for n in ops.ospf_neighbors() if n.state.lower().startswith("full")
            ]
            bindings = ops.ospf_bindings()
            binding = next(
                (
                    key
                    for key, interfaces in bindings.items()
                    if sum(n.interface in interfaces for n in neighbors) == 1
                ),
                None,
            )
            if binding is None:
                raise RuntimeCapabilityError(
                    f"No OSPF binding on {params.host_name!r} covers exactly one Full adjacency."
                )
            self._saved_config = ops.save_config()
            self._native_bindings = bindings
            self._native_neighbors = {
                (n.router_id, n.address, n.interface) for n in neighbors
            }
            self._dropped_native_binding = binding
            ops.remove_ospf_binding(binding)
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
        ops = native_fault_ops(self, params.host_name, "ospf_bindings")
        if ops is not None:
            before = getattr(self, "_native_bindings", {})
            dropped = getattr(self, "_dropped_native_binding", None)
            running = ops.ospf_bindings()
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(dropped)
                and running == {k: v for k, v in before.items() if k != dropped},
                details={
                    "host": params.host_name,
                    "dropped_binding": dropped,
                    "bindings_before": len(before),
                    "bindings_after": len(running),
                },
            )
        kind, binding, area = getattr(self, "_dropped_binding", None) or (
            None,
            None,
            None,
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

    def recover_fault(self, params: OSPFNeighborMissingParams) -> dict:
        """Restore the binding and wait for Full neighbors and OSPF-learned routes."""
        ops = native_fault_ops(self, params.host_name, "restore_config")
        if ops is not None:
            saved = getattr(self, "_saved_config", None)
            if not saved:
                return {"verified": False, "details": {"error": "no_saved_config"}}
            ops.restore_config(saved)

            def healthy():
                full = {
                    (n.router_id, n.address, n.interface)
                    for n in ops.ospf_neighbors()
                    if n.state.lower().startswith("full")
                }
                # Restoring RouterOS templates can allocate new internal IDs.
                return (
                    full == self._native_neighbors
                    and self._original_routes <= self._route_prefixes(params.host_name)
                )
        else:
            dropped = getattr(self, "_dropped_binding", None)
            if not dropped:
                return {"verified": False, "details": {"error": "no_saved_binding"}}
            kind, binding, area = dropped
            lines = (
                ("router ospf", f"network {binding} area {area}")
                if kind == "network"
                else (f"interface {binding}", f"ip ospf area {area}")
            )
            self.runtime.exec(params.host_name, frr_config_command(*lines))
            frr_save_config(self.runtime, params.host_name)

            def healthy():
                raw = self.runtime.exec(
                    params.host_name, "vtysh -c 'show ip ospf neighbor json'"
                )
                return (
                    bool(self._frr_neighbors)
                    and _full_frr_neighbors(raw) == self._frr_neighbors
                    and self._original_routes <= self._route_prefixes(params.host_name)
                )

        deadline = time.monotonic() + 120
        while True:
            ok = healthy()
            if ok or time.monotonic() >= deadline:
                return {
                    "verified": ok,
                    "details": {
                        "host": params.host_name,
                        "adjacencies_restored": ok,
                        "original_route_count": len(self._original_routes),
                        "routes_restored": ok,
                    },
                }
            time.sleep(3)


# ==================================================================
# Problem: OSPF Duplicate Router ID
# ==================================================================

_ROUTER_LSA_SECTION_RE = re.compile(r"Router Link States \(Area ([\d.]+)")
_LSA_ROW_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+\d+\s+0x")
_ROUTER_ID_LINE_RE = r"^[[:space:]]*(ospf )?router-id "


def router_lsa_origins(database: str) -> dict[str, set[str]]:
    """Area -> advertising routers of its router-LSAs, from ``show ip ospf database``."""
    origins: dict[str, set[str]] = {}
    area = None
    for line in database.splitlines():
        if match := _ROUTER_LSA_SECTION_RE.search(line):
            area = match[1]
            origins.setdefault(area, set())
        elif "Link States" in line:
            area = None
        elif area and (match := _LSA_ROW_RE.match(line.strip())):
            origins[area].add(match[2])
    return origins


def _ospf_router_id(raw_json: str) -> str | None:
    try:
        return json.loads(raw_json).get("routerId")
    except (ValueError, AttributeError):
        return None


class OSPFDuplicateRouterIdParams(BaseModel):
    """Parameters for injecting an OSPF router ID that another router already uses."""

    host_name: str = Field(description="Target router host name.")


class OSPFDuplicateRouterId(ProblemBase):
    """The router takes the OSPF router ID of another router in its area.

    With a non-adjacent router, both originate a router-LSA under the same
    ID. They may re-flood it against each other for a few minutes, and in
    the end every router keeps one of the two LSAs, so the links and subnets
    of one of the two routers drop out of every routing table while all
    adjacencies stay Full. Which router loses depends on LSA timing. With a
    neighbor, the neighbor ignores hellos that carry its own ID and the
    adjacency never forms. Inject prefers the lowest non-adjacent router ID
    in the target's areas and otherwise takes the lowest neighbor ID, so the
    choice is reproducible. See Cisco, "Troubleshooting Duplicate Router IDs
    with OSPF":
    https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/23862-duplicate-router-id-ospf.html
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "ospf_duplicate_router_id"
    description = "OSPF router ID duplicates the router ID of another router."
    effect_protocol = "ospf"
    TAGS = ["ospf"]
    supported_backends = ("kathara",)
    Params = OSPFDuplicateRouterIdParams
    native_router_operations = frozenset(
        {
            "ospf_router_id",
            "ospf_router_lsa_ids",
            "ospf_neighbor_ids",
            "ospf_neighbors",
            "set_ospf_router_id",
            "ospf_routes",
            "connected_routes",
            "save_config",
            "restore_config",
        }
    )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.problems.support.benchmark_targets import choice

        if cls._has_declared_stub(ctx.net_env, ctx.router0):
            return {"host_name": ctx.router0}
        targets = [r for r in ctx.routers if cls._has_declared_stub(ctx.net_env, r)]
        return {"host_name": choice(ctx.rng, targets, ctx.router0)}

    @staticmethod
    def _has_declared_stub(net_env, host: str) -> bool:
        from nika.net_env.native_routers import router_sketches

        plan = getattr(net_env, "plan", None)
        if plan is not None:
            return plan.igp == "ospf" and any(
                node.device_name == host
                and any(not interface.passive for interface in node.interfaces)
                for node in plan.nodes
            )
        sketches = router_sketches(net_env)
        connected = {
            router: {
                ipaddress.ip_interface(cidr).network
                for cidr in re.findall(r"ip addr add ([0-9./]+) dev", startup)
            }
            for router, (_, startup) in sketches.items()
        }
        own = connected.get(host, set())
        others = set().union(
            *(prefixes for r, prefixes in connected.items() if r != host)
        )
        networks, _ = _ospf_area_bindings(sketches.get(host, ("", ""))[0])
        return any(
            prefix.subnet_of(ipaddress.ip_network(network))
            for prefix in own - others
            for network, _ in networks
        )

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        host = inject.get("host_name")
        require_node_capability(ctx, cls.root_cause_name, host, "ospf")
        if not cls._has_declared_stub(ctx.net_env, host):
            raise ValueError(
                f"{cls.root_cause_name} needs a router with an OSPF stub subnet"
            )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFDuplicateRouterIdParams):
        return [node_resource(params.host_name)]

    def _router_id(self, host: str) -> str | None:
        ops = native_fault_ops(self, host, "ospf_router_id")
        if ops is not None:
            return ops.ospf_router_id()
        return _ospf_router_id(
            self.runtime.exec(host, "vtysh -c 'show ip ospf json' 2>/dev/null")
        )

    def _duplicate_candidate(self, host: str, own: str) -> str:
        ops = native_fault_ops(self, host, "ospf_router_lsa_ids")
        if ops is not None:
            neighbors = ops.ospf_neighbor_ids()
            candidates = (ops.ospf_router_lsa_ids() - neighbors - {own}) or (
                neighbors - {own}
            )
            if not candidates:
                raise RuntimeCapabilityError(
                    f"No other OSPF router shares an area with {host!r}."
                )
            return min(candidates, key=ipaddress.ip_address)
        database = self.runtime.exec(
            host, "vtysh -c 'show ip ospf database' 2>/dev/null"
        )
        raw = self.runtime.exec(
            host, "vtysh -c 'show ip ospf neighbor json' 2>/dev/null"
        )
        try:
            neighbors = set((json.loads(raw).get("neighbors") or {}).keys())
        except (ValueError, AttributeError):
            neighbors = set()
        in_area = set().union(*router_lsa_origins(database).values()) - {own}
        candidates = (in_area - neighbors) or (neighbors - {own})
        if not candidates:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: no other OSPF router shares an area "
                f"with {host!r}."
            )
        return min(candidates, key=lambda rid: ipaddress.ip_address(rid))

    def _set_router_id(self, host: str, router_id: str | None) -> None:
        """Apply ``router_id`` (``None``: derive it) to ospfd and frr.conf."""
        line = f"ospf router-id {router_id}" if router_id else "no ospf router-id"
        self.runtime.exec(host, frr_config_command("router ospf", line))
        # A new router ID takes effect only when the OSPF process restarts.
        self.runtime.exec(host, "vtysh -c 'clear ip ospf process' >/dev/null 2>&1")
        if router_id is None:
            edit = f"'{_OSPF_BLOCK_RANGE} {{/{_ROUTER_ID_LINE_RE}/d}}'"
        elif self._file_has_router_id:
            edit = f"'{_OSPF_BLOCK_RANGE} s/{_ROUTER_ID_LINE_RE}\\S+/ \\1router-id {router_id}/'"
        else:
            edit = f"'/^router ospf$/a\\ ospf router-id {router_id}'"
        self.runtime.exec(host, f"sed -i -E {edit} {FRR_CONF}")

    def inject_fault(self, params: OSPFDuplicateRouterIdParams):
        own = self._router_id(params.host_name)
        if not own:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: OSPF is not running on {params.host_name!r}."
            )
        self.duplicate_router_id = self._duplicate_candidate(params.host_name, own)
        self._twin_host = next(
            (
                router
                for router in self.net_env.routers or []
                if router != params.host_name
                and self._router_id(router) == self.duplicate_router_id
            ),
            None,
        )
        self._healthy_router_id = own
        self._healthy_routing = {
            host: _ospf_health(self, host)
            for host in (params.host_name, self._twin_host)
            if host is not None
        }
        ops = native_fault_ops(self, params.host_name, "set_ospf_router_id")
        if ops is not None:
            self._saved_config = ops.save_config()
            self._orig_router_id = own
            ops.set_ospf_router_id(self.duplicate_router_id)
            return
        config = self.runtime.exec(params.host_name, f"cat {FRR_CONF}")
        self._file_has_router_id = bool(
            re.search(r"^\s*(ospf )?router-id ", _ospf_block(config), re.M)
        )
        self._orig_router_id = own if self._file_has_router_id else None
        self._set_router_id(params.host_name, self.duplicate_router_id)
        self.logger.info(
            f"Injected OSPF router ID {self.duplicate_router_id} on "
            f"{params.host_name} (was {own})."
        )

    def verify_fault(self, params: OSPFDuplicateRouterIdParams) -> dict:
        expected = getattr(self, "duplicate_router_id", None)
        running = self._router_id(params.host_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=expected is not None and running == expected,
            details={
                "host": params.host_name,
                "running_router_id": running,
                "duplicate_router_id": expected,
            },
        )

    def recover_fault(self, params: OSPFDuplicateRouterIdParams) -> dict:
        baseline = getattr(self, "_healthy_routing", None)
        if not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        ops = native_fault_ops(self, params.host_name, "restore_config")
        if ops is not None:
            saved = getattr(self, "_saved_config", None)
            if saved:
                ops.set_ospf_router_id(self._orig_router_id)
                ops.restore_config(saved)
        else:
            self._set_router_id(
                params.host_name, getattr(self, "_orig_router_id", None)
            )
        # The twin that originally owned this ID can keep a stale LSA until
        # its OSPF process restarts, so stub prefixes never return.
        twin = getattr(self, "_twin_host", None)
        if twin and native_router_profile(self.net_env, twin) is None:
            self.runtime.exec(twin, "vtysh -c 'clear ip ospf process' >/dev/null 2>&1")

        def healthy():
            if self._router_id(params.host_name) != self._healthy_router_id:
                return False
            for host, before in baseline.items():
                current = _ospf_health(self, host)
                if any(not values <= current[key] for key, values in before.items()):
                    return False
            return True

        restored = wait_until(healthy, 240, interval_s=3)
        running = self._router_id(params.host_name)
        return {
            "verified": restored and running == self._healthy_router_id,
            "details": {"host": params.host_name, "running_router_id": running},
        }


# ==================================================================
# Problem: OSPF network type mismatch
# ==================================================================


_FRR_NETWORK_TYPE = {
    "BROADCAST": "broadcast",
    "POINTOPOINT": "point-to-point",
    "NBMA": "non-broadcast",
    "POINTOMULTIPOINT": "point-to-multipoint",
}


def normalize_ospf_network_type(raw: str) -> str:
    """Map a platform token to ``broadcast``, ``point-to-point``, or ``non-broadcast``."""
    token = raw.lower().replace("_", "-").replace(" ", "")
    if token in {"point-to-point", "pointtopoint", "ptp", "p2p"}:
        return "point-to-point"
    if token in {"non-broadcast", "nonbroadcast", "nbma"}:
        return "non-broadcast"
    if token == "broadcast":
        return "broadcast"
    return _FRR_NETWORK_TYPE.get(raw.upper(), raw.lower())


def mismatch_ospf_network_type(current: str) -> str:
    """The other of broadcast-style and point-to-point."""
    if normalize_ospf_network_type(current) == "point-to-point":
        return "broadcast"
    return "point-to-point"


def ospf_router_links(net_env) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """Two-endpoint router-router links ``((a, ia), (b, ib))`` from the topology."""
    routers = set(net_env.routers or [])
    plan = getattr(net_env, "plan", None)
    active = None
    if plan is not None:
        from nika.net_env.isp.igp.plan import active_igp_links

        if plan.igp != "ospf":
            return []
        active = {
            frozenset(
                ((link.endpoint_a, link.iface_a), (link.endpoint_b, link.iface_b))
            )
            for link in active_igp_links(plan)
        }
    links = []
    for _key, tps in iter_link_termination_points(net_env):
        ends = [
            (node, intf) for node, intf in map(parse_endpoint, tps) if node and intf
        ]
        router_ends = [(node, intf) for node, intf in ends if node in routers]
        if len(ends) != 2 or len(router_ends) != 2:
            continue
        (left, left_intf), (right, right_intf) = router_ends
        if left == right:
            continue
        if active is not None and frozenset(router_ends) not in active:
            continue
        links.append(((left, left_intf), (right, right_intf)))
    return links


def ospf_cut_edge_interfaces(net_env) -> list[tuple[str, str]]:
    """Interfaces of router links whose far router has only one router neighbor."""
    links = ospf_router_links(net_env)
    degree: dict[str, int] = {}
    for (left, _), (right, _) in links:
        degree[left] = degree.get(left, 0) + 1
        degree[right] = degree.get(right, 0) + 1
    targets = {
        endpoint
        for pair in links
        for endpoint, peer in (pair, pair[::-1])
        if degree.get(peer[0], 0) == 1 or degree.get(endpoint[0], 0) == 1
    }
    return sorted(targets)


def ospf_link_peer(net_env, host: str, intf: str) -> tuple[str, str] | None:
    """The router on the other end of ``host:intf``, if the link is router-router."""
    for (left, left_intf), (right, right_intf) in ospf_router_links(net_env):
        if (left, left_intf) == (host, intf):
            return right, right_intf
        if (right, right_intf) == (host, intf):
            return left, left_intf
    return None


def ospf_stub_router(net_env, host: str, intf: str) -> str | None:
    """The degree-1 router of the cut-edge that contains ``host:intf``."""
    peer = ospf_link_peer(net_env, host, intf)
    if peer is None:
        return None
    degree: dict[str, int] = {}
    for (left, _), (right, _) in ospf_router_links(net_env):
        degree[left] = degree.get(left, 0) + 1
        degree[right] = degree.get(right, 0) + 1
    if degree.get(host, 0) == 1:
        return host
    if degree.get(peer[0], 0) == 1:
        return peer[0]
    return None


def hosts_behind_router(net_env, router: str) -> list[str]:
    """Hosts and services reached from ``router`` without crossing another router."""
    from collections import deque

    from nika.runtime.spec import NodeRole

    routers = set(net_env.routers or [])
    identities = getattr(net_env, "machine_identities", None) or {}
    adjacency: dict[str, set[str]] = {}
    for _key, tps in iter_link_termination_points(net_env):
        nodes = [parse_endpoint(tp)[0] for tp in tps if parse_endpoint(tp)[0]]
        for node in nodes:
            adjacency.setdefault(node, set()).update(n for n in nodes if n != node)
    seen = {router}
    queue = deque([router])
    hosts = []
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency.get(current, ())):
            if neighbor in seen or neighbor in routers:
                continue
            seen.add(neighbor)
            role = getattr(identities.get(neighbor), "role", None)
            if role in {NodeRole.HOST, NodeRole.SERVICE}:
                hosts.append(neighbor)
            else:
                queue.append(neighbor)
    return hosts


class OSPFNetworkTypeMismatchParams(BaseModel):
    """Parameters for injecting an OSPF network-type disagreement."""

    host_name: str = Field(description="Target router host name.")
    intf_name: str = Field(description="OSPF interface toward the neighbor.")


class OSPFNetworkTypeMismatch(ProblemBase):
    """One OSPF interface uses a different network type than its neighbor.

    Broadcast (or NBMA) and point-to-point hellos can still bring the
    adjacency to Full when hello and dead timers match, but one router-LSA
    describes a transit network and the other a point-to-point link. SPF's
    two-way check then rejects the link, so routes across it disappear.
    See Cisco, "Troubleshoot Open Shortest Path First Route Database Issues":
    https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/7112-26.html
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "ospf_network_type_mismatch"
    description = "OSPF interface network type does not match the neighbor."
    effect_protocol = "ospf"
    TAGS = ["ospf"]
    supported_backends = ("kathara",)
    native_router_operations = frozenset(
        {
            "ospf_network_type",
            "set_ospf_network_type",
            "ospf_interface_neighbor_full",
            "ospf_neighbors",
            "ospf_routes",
        }
    )
    Params = OSPFNetworkTypeMismatchParams

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        # XRd drops the adjacency when network type disagrees, so the
        # Full-but-unrouted symptom does not appear.
        if device_profile == "iosxr":
            return False
        return super().runs_on_device_profile(device_profile)

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = ospf_cut_edge_interfaces(ctx.net_env)
        host, intf = ctx.rng.choice(targets or [(ctx.router0, "eth0")])
        return {"host_name": host, "intf_name": intf}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        host = inject.get("host_name")
        intf = inject.get("intf_name")
        if (host, intf) not in set(ospf_cut_edge_interfaces(ctx.net_env)):
            raise ValueError(
                f"{cls.root_cause_name} needs a cut-edge OSPF interface, "
                f"not {host!r} {intf!r}"
            )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFNetworkTypeMismatchParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def _frr_network_type(self, host: str, intf: str) -> str | None:
        raw = self.runtime.exec(
            host, "vtysh -c 'show ip ospf interface json' 2>/dev/null"
        )
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        entry = (data.get("interfaces") or {}).get(intf) or {}
        token = entry.get("networkType")
        return normalize_ospf_network_type(str(token)) if token else None

    def _set_frr_network_type(self, host: str, intf: str, network_type: str) -> None:
        self.runtime.exec(
            host,
            frr_config_command(f"interface {intf}", f"ip ospf network {network_type}"),
        )
        frr_save_config(self.runtime, host)

    def inject_fault(self, params: OSPFNetworkTypeMismatchParams):
        host, intf = params.host_name, params.intf_name
        peer = ospf_link_peer(self.net_env, host, intf)
        if peer is None:
            raise RuntimeCapabilityError(f"No OSPF router peer for {host!r} {intf!r}")
        self._healthy_routing = {
            node: _ospf_health(self, node) for node in (host, peer[0])
        }
        ops = native_fault_ops(self, host, "set_ospf_network_type")
        if ops is not None:
            current = normalize_ospf_network_type(ops.ospf_network_type(intf))
            injected = mismatch_ospf_network_type(current)
            self._orig_network_type = current
            self.injected_network_type = injected
            ops.set_ospf_network_type(intf, injected)
        else:
            current = self._frr_network_type(host, intf)
            if not current:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__}: OSPF is not enabled on {host!r} {intf!r}."
                )
            injected = mismatch_ospf_network_type(current)
            self._orig_network_type = current
            self.injected_network_type = injected
            self._set_frr_network_type(host, intf, injected)
        self.logger.info(
            f"Set OSPF network type {injected} on {host} {intf} (was {current})."
        )

    def verify_fault(self, params: OSPFNetworkTypeMismatchParams) -> dict:
        host, intf = params.host_name, params.intf_name
        expected = getattr(self, "injected_network_type", None)
        ops = native_fault_ops(self, host, "ospf_network_type")
        running = (
            normalize_ospf_network_type(ops.ospf_network_type(intf))
            if ops is not None
            else self._frr_network_type(host, intf)
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=expected is not None and running == expected,
            details={
                "host": host,
                "intf": intf,
                "running_network_type": running,
                "injected_network_type": expected,
            },
        )

    def recover_fault(self, params: OSPFNetworkTypeMismatchParams) -> dict:
        host, intf = params.host_name, params.intf_name
        orig = getattr(self, "_orig_network_type", None)
        baseline = getattr(self, "_healthy_routing", None)
        if not orig or not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        ops = native_fault_ops(self, host, "set_ospf_network_type")
        if ops is not None:
            ops.set_ospf_network_type(intf, orig)
        else:
            self._set_frr_network_type(host, intf, orig)

        def healthy():
            for node, before in baseline.items():
                current = _ospf_health(self, node)
                if any(not values <= current[key] for key, values in before.items()):
                    return False
            return True

        restored = wait_until(healthy, 120, interval_s=3)
        running = (
            normalize_ospf_network_type(ops.ospf_network_type(intf))
            if ops is not None
            else self._frr_network_type(host, intf)
        )
        return {
            "verified": restored and running == orig,
            "details": {
                "host": host,
                "intf": intf,
                "running_network_type": running,
            },
        }


class OSPFHelloIntervalMismatchParams(BaseModel):
    """Parameters for an OSPF interface timer disagreement."""

    host_name: str = Field(description="Target router host name.")
    intf_name: str = Field(description="OSPF interface toward the neighbor.")


class OSPFHelloIntervalMismatch(ProblemBase):
    """One interface advertises hello and dead intervals its neighbor rejects.

    RFC 2328 section 10.5 requires both HelloInterval and RouterDeadInterval
    to match before a received Hello can refresh the neighbor. Changing one
    side therefore expires the adjacency while the interface stays up.
    https://www.rfc-editor.org/rfc/rfc2328#section-10.5
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "ospf_hello_interval_mismatch"
    description = "OSPF interface hello and dead intervals differ from the neighbor."
    symptom_probe = "custom"
    symptom_class = "control_plane"
    effect_protocol = "ospf"
    TAGS = ["ospf"]
    COMPATIBLE_COLUMNS = None
    supported_backends = ("kathara",)
    native_router_operations = frozenset(
        {
            "ospf_interface_timers",
            "set_ospf_interface_timers",
            "ospf_interface_neighbor_full",
            "ospf_routes",
        }
    )
    Params = OSPFHelloIntervalMismatchParams
    BENCHMARK_TARGETS = "canonical"

    @staticmethod
    def _targets(net_env):
        return [
            (host, intf)
            for host, intf in ospf_cut_edge_interfaces(net_env)
            if hosts_behind_router(net_env, ospf_stub_router(net_env, host, intf))
        ]

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = cls._targets(ctx.net_env)
        host, intf = ctx.rng.choice(targets or [(ctx.router0, "eth0")])
        return {"host_name": host, "intf_name": intf}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        host, intf = inject.get("host_name"), inject.get("intf_name")
        require_node_capability(ctx, cls.root_cause_name, host, "ospf")
        if (host, intf) not in cls._targets(ctx.net_env):
            raise ValueError(
                f"{cls.root_cause_name} needs an OSPF cut edge serving hosts"
            )

    def root_cause_resources(self, params: OSPFHelloIntervalMismatchParams):
        return [node_resource(params.host_name)]

    def _timers(self, host: str, intf: str) -> tuple[int, int]:
        ops = native_fault_ops(self, host, "ospf_interface_timers")
        if ops is not None:
            return ops.ospf_interface_timers(intf)
        output = self.runtime.exec(host, f"vtysh -c 'show ip ospf interface {intf}'")
        match = re.search(
            r"Timer intervals configured, Hello (\d+)s?, Dead (\d+)s?", output
        )
        if not match:
            raise RuntimeCapabilityError(f"No OSPF timers on {host!r} {intf!r}")
        return int(match[1]), int(match[2])

    def _set_timers(self, host: str, intf: str, timers: tuple[int, int]) -> None:
        hello, dead = timers
        ops = native_fault_ops(self, host, "set_ospf_interface_timers")
        if ops is not None:
            ops.set_ospf_interface_timers(intf, hello, dead)
        else:
            self.runtime.exec(
                host,
                frr_config_command(
                    f"interface {intf}",
                    f"ip ospf hello-interval {hello}",
                    f"ip ospf dead-interval {dead}",
                ),
            )
            frr_save_config(self.runtime, host)

    def _full(self, host: str, intf: str) -> bool:
        ops = native_fault_ops(self, host, "ospf_interface_neighbor_full")
        if ops is not None:
            return ops.ospf_interface_neighbor_full(intf)
        raw = self.runtime.exec(host, "vtysh -c 'show ip ospf neighbor json'")
        neighbors = json.loads(raw).get("neighbors", {})
        return any(
            row.get("ifaceName", "").split(":")[0] == intf
            and row.get("nbrState", "").lower().startswith("full")
            for rows in neighbors.values()
            for row in (rows if isinstance(rows, list) else [rows])
            if isinstance(row, dict)
        )

    def _routes(self, host: str) -> set[str]:
        ops = native_fault_ops(self, host, "ospf_routes")
        if ops is not None:
            return ops.ospf_routes()
        raw = self.runtime.exec(host, "vtysh -c 'show ip route ospf json'")
        return {
            prefix
            for prefix, routes in json.loads(raw).items()
            if any(row.get("selected") for row in routes)
        }

    def link_health(self, params: OSPFHelloIntervalMismatchParams) -> dict:
        """Read adjacency, link state, and routes at both ends of the selected link."""
        host, intf = params.host_name, params.intf_name
        peer = ospf_link_peer(self.net_env, host, intf)
        if peer is None:
            raise RuntimeCapabilityError(f"No OSPF router peer for {host!r} {intf!r}")
        return {
            node: {
                "full": self._full(node, interface),
                "interface_up": self.runtime.get_interface_operstate(node, interface)
                == "up",
                "routes": sorted(self._routes(node)),
            }
            for node, interface in ((host, intf), peer)
        }

    def inject_fault(self, params: OSPFHelloIntervalMismatchParams):
        host, intf = params.host_name, params.intf_name
        if (host, intf) not in self._targets(self.net_env):
            raise RuntimeCapabilityError(
                "OSPF timer mismatch needs a cut edge serving hosts"
            )
        peer = ospf_link_peer(self.net_env, host, intf)
        original = self._timers(host, intf)
        if original != self._timers(*peer):
            raise RuntimeCapabilityError("OSPF interface timers already disagree")
        self._healthy_link = self.link_health(params)
        if not all(
            row["full"] and row["interface_up"] for row in self._healthy_link.values()
        ):
            raise RuntimeCapabilityError(
                "OSPF timer mismatch needs a healthy Full adjacency"
            )
        if not any(row["routes"] for row in self._healthy_link.values()):
            raise RuntimeCapabilityError("OSPF cut edge has no learned routes")
        self._original_timers = original
        self.injected_timers = next(
            timers
            for timers in ((5, 20), (10, 40), (15, 60))
            if timers[0] != original[0] and timers[1] != original[1]
        )
        self._set_timers(host, intf, self.injected_timers)

    def verify_fault(self, params: OSPFHelloIntervalMismatchParams) -> dict:
        running = self._timers(params.host_name, params.intf_name)
        peer = ospf_link_peer(self.net_env, params.host_name, params.intf_name)
        neighbor = self._timers(*peer) if peer else None
        expected = getattr(self, "injected_timers", None)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=expected is not None
            and running == expected
            and running != neighbor,
            details={
                "host": params.host_name,
                "intf": params.intf_name,
                "running_timers": running,
                "neighbor_timers": neighbor,
            },
        )

    def recover_fault(self, params: OSPFHelloIntervalMismatchParams) -> dict:
        original = getattr(self, "_original_timers", None)
        baseline = getattr(self, "_healthy_link", None)
        if original is None or not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        self._set_timers(params.host_name, params.intf_name, original)
        deadline = time.monotonic() + 120
        while True:
            health = self.link_health(params)
            restored = all(
                row["full"]
                and row["interface_up"]
                and set(baseline[node]["routes"]) <= set(row["routes"])
                for node, row in health.items()
            )
            if restored or time.monotonic() >= deadline:
                break
            time.sleep(3)
        running = self._timers(params.host_name, params.intf_name)
        return {
            "verified": restored and running == original,
            "details": {"running_timers": running, "link": health},
        }


# ==================================================================
# Problem: OSPF area range suppression
# ==================================================================


def ospf_area_branches(net_env) -> dict[tuple[str, str], dict]:
    """Non-backbone components with exactly one ABR and an exact prefix cover."""
    from nika.net_env.native_routers import router_sketches

    bindings = {}
    for host, (config, startup) in router_sketches(net_env).items():
        addresses = re.findall(r"ip addr add ([0-9./]+) dev", startup)
        bindings[host] = {
            (
                str(ipaddress.IPv4Interface(address).network),
                str(ipaddress.IPv4Address(int(area) if area.isdigit() else area)),
            )
            for address in addresses
            for network, area in ospf_networks(config)
            if ipaddress.IPv4Interface(address).ip in ipaddress.IPv4Network(network)
        }
    areas = {area for rows in bindings.values() for _, area in rows} - {"0.0.0.0"}
    result = {}
    for area in sorted(areas):
        remaining = {
            host for host, rows in bindings.items() if any(a == area for _, a in rows)
        }
        while remaining:
            component = {min(remaining)}
            while True:
                prefixes = {
                    p for host in component for p, a in bindings[host] if a == area
                }
                expanded = component | {
                    host
                    for host in remaining
                    if any(a == area and p in prefixes for p, a in bindings[host])
                }
                if expanded == component:
                    break
                component = expanded
            remaining -= component
            abrs = [
                host
                for host in component
                if any(a == "0.0.0.0" for _, a in bindings[host])
            ]
            observers = sorted(set(bindings) - component)
            if len(abrs) != 1 or not observers:
                continue
            host = abrs[0]
            result[host, area] = {
                "area": area,
                "members": sorted(component),
                "observers": observers,
                "prefixes": sorted(prefixes),
                "ranges": [
                    str(p)
                    for p in ipaddress.collapse_addresses(
                        ipaddress.IPv4Network(p) for p in prefixes
                    )
                ],
                "interfaces": {
                    r: sorted(
                        {
                            intf
                            for left, right in ospf_router_links(net_env)
                            for node, intf in (left, right)
                            if node == r
                        }
                    )
                    for r in bindings
                },
                "backbone_observer": next(
                    (
                        r
                        for r in observers
                        if all(a == "0.0.0.0" for _, a in bindings[r])
                    ),
                    None,
                ),
            }
    return result


class OspfAreaRangeSuppressionParams(BaseModel):
    host_name: str = Field(description="Target area border router.")
    area: str = Field(description="Non-backbone OSPF area ID.")


class OspfAreaRangeSuppression(ProblemBase):
    """An ABR suppresses the exact subnets of a non-backbone area component.

    RFC 2328 section 12.4.3 and Appendix C.2 define DoNotAdvertise area
    ranges: the ABR originates no Type-3 LSAs for their component networks.
    https://www.rfc-editor.org/rfc/rfc2328#section-12.4.3
    FRR: https://docs.frrouting.org/en/latest/ospfd.html#ospf-area
    IOS XR: https://www.cisco.com/c/en/us/td/docs/routers/asr9000/software/routing/command/reference/b-routing-cr-asr9000/ospf-commands.html
    RouterOS: https://help.mikrotik.com/docs/spaces/ROS/pages/331612216/routing+ospf
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "ospf_area_range_suppression"
    description = "An OSPF area border router has suppressing area address ranges."
    symptom_desc = "Hosts in one distribution branch cannot reach other areas."
    symptom_probe = "custom"
    symptom_class = "control_plane"
    effect_protocol = "ospf"
    TAGS = ["ospf"]
    COMPATIBLE_COLUMNS = frozenset({"campus_lan"})
    supported_backends = ("kathara",)
    BENCHMARK_TARGETS = "canonical"
    Params = OspfAreaRangeSuppressionParams
    native_router_operations = frozenset(
        {
            "ospf_area_ranges",
            "set_ospf_area_ranges",
            "ospf_summary_lsa_ids",
            "ospf_routes",
            "connected_routes",
            "ospf_interface_neighbor_full",
        }
    )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = sorted(ospf_area_branches(ctx.net_env))
        if not targets:
            raise ValueError(
                "OSPF range suppression needs a non-backbone branch with a single ABR"
            )
        host, area = ctx.rng.choice(targets)
        return {"host_name": host, "area": area}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        area = str(inject.get("area", ""))
        area = str(ipaddress.IPv4Address(int(area) if area.isdigit() else area))
        if (inject.get("host_name"), area) not in ospf_area_branches(ctx.net_env):
            raise ValueError(
                "OSPF range suppression needs a non-backbone branch with a single ABR"
            )

    def root_cause_resources(self, params):
        return [node_resource(params.host_name)]

    def branch(self, params):
        area = params.area
        area = str(ipaddress.IPv4Address(int(area) if area.isdigit() else area))
        branch = ospf_area_branches(self.net_env).get((params.host_name, area))
        if not branch or not branch["backbone_observer"]:
            raise RuntimeCapabilityError(
                "No independently advertised OSPF branch for this ABR and area"
            )
        return branch

    def _ranges(self, host, area):
        ops = native_fault_ops(self, host, "ospf_area_ranges")
        if ops is not None:
            return ops.ospf_area_ranges(area)
        config = self.runtime.exec(host, "vtysh -c 'show running-config'")
        return {
            prefix: "not-advertise" in options.split()
            for a, prefix, options in re.findall(
                r"^ area (\S+) range (\S+)([^\n]*)", _ospf_block(config), re.M
            )
            if str(ipaddress.IPv4Address(int(a) if a.isdigit() else a)) == area
        }

    def _set_ranges(self, host, area, ranges, suppress):
        ops = native_fault_ops(self, host, "set_ospf_area_ranges")
        if ops is not None:
            ops.set_ospf_area_ranges(area, ranges, suppress)
        else:
            lines = [
                f"area {area} range {prefix} not-advertise"
                if suppress
                else f"no area {area} range {prefix}"
                for prefix in ranges
            ]
            self.runtime.exec(host, frr_config_command("router ospf", *lines))
            frr_save_config(self.runtime, host)

    def inject_fault(self, params):
        from nika.validation.symptom.ospf_range_probes import area_range_baseline

        branch = self.branch(params)
        if self._ranges(params.host_name, branch["area"]):
            raise RuntimeCapabilityError(
                "Target OSPF area already has configured ranges"
            )
        # Normal injection also rejects a target with no healthy inter-area routes.
        baseline = getattr(self, "_audit_ospf_range", None)
        if not baseline or baseline["host"] != params.host_name:
            ok, details = area_range_baseline(self, params)
            if not ok:
                raise RuntimeCapabilityError(
                    f"OSPF branch has no healthy suppression baseline: {details}"
                )
        self.suppressed_ranges = branch["ranges"]
        self._set_ranges(params.host_name, branch["area"], self.suppressed_ranges, True)

    def verify_fault(self, params):
        branch = self.branch(params)
        running = self._ranges(params.host_name, branch["area"])
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=all(running.get(p) is True for p in branch["ranges"]),
            details={
                "host": params.host_name,
                "area": branch["area"],
                "ranges": running,
            },
        )

    def recover_fault(self, params):
        from nika.validation.symptom.ospf_range_probes import area_range_restored

        branch = self.branch(params)
        self._set_ranges(
            params.host_name,
            branch["area"],
            getattr(self, "suppressed_ranges", []),
            False,
        )
        ok, details = area_range_restored(self, params)
        running = self._ranges(params.host_name, branch["area"])
        removed = not any(p in running for p in branch["ranges"])
        return {"verified": ok and removed, "details": details}
