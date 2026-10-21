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

    def inject_fault(self, params: OSPFAreaMisconfigParams):
        running_cfg = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config'"
        )
        statements = ospf_networks(running_cfg)
        if not statements:
            raise RuntimeError(f"Could not find OSPF area on {params.host_name}")
        correct_area = statements[0][1]
        wrong_area = "66" if correct_area != "66" else "99"
        # Keep the original area set in memory; a .bak file would expose it.
        self._orig_areas = {area for _prefix, area in statements}
        # Move the statements in the running daemon (no restart), then keep the
        # startup file consistent with it.
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
        self.logger.info(
            f"Injected OSPF area misconfiguration on {params.host_name} from area {correct_area} to {wrong_area}."
        )

    def verify_fault(self, params: OSPFAreaMisconfigParams) -> dict:
        """Verify the OSPF area in frr.conf and in the running daemon was changed."""
        file_areas_raw = self.runtime.exec(
            params.host_name,
            "grep -E '^[[:space:]]*network .* area ' /etc/frr/frr.conf 2>/dev/null | awk '{print $NF}' | sort -u",
        ).strip()
        running_areas_raw = self.runtime.exec(
            params.host_name,
            "vtysh -c 'show running-config' 2>/dev/null | grep -E '^[[:space:]]*network .* area ' | awk '{print $NF}' | sort -u",
        ).strip()
        file_areas = set(file_areas_raw.splitlines()) if file_areas_raw else set()
        orig_areas = set(getattr(self, "_orig_areas", None) or ())
        running_areas = (
            set(running_areas_raw.splitlines()) if running_areas_raw else set()
        )
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

    def _single_adjacency_network(self, host: str) -> tuple[str, str]:
        """The first ``network`` statement that covers exactly one Full adjacency."""
        statements = ospf_networks(
            self.runtime.exec(host, "vtysh -c 'show running-config'")
        )
        raw = self.runtime.exec(host, "vtysh -c 'show ip ospf neighbor json'")
        try:
            neighbors = json.loads(raw).get("neighbors") or {}
        except (ValueError, AttributeError):
            neighbors = {}
        local_ips = [
            ipaddress.ip_address(str(entry.get("ifaceName", "")).split(":")[-1])
            for entries in neighbors.values()
            for entry in (entries if isinstance(entries, list) else [entries])
            if str(entry.get("nbrState", "")).startswith("Full")
            and ":" in str(entry.get("ifaceName", ""))
        ]
        for prefix, area in statements:
            network = ipaddress.ip_network(prefix, strict=False)
            if sum(ip in network for ip in local_ips) == 1:
                return prefix, area
        raise RuntimeCapabilityError(
            f"{type(self).__name__}: no OSPF network statement on {host!r} "
            "covers exactly one Full adjacency."
        )

    def inject_fault(self, params: OSPFNeighborMissingParams):
        # Drop the one statement that brings a single adjacency into OSPF, the
        # way a deleted ``network`` line loses that neighbor and only it.
        prefix, area = self._single_adjacency_network(params.host_name)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'configure terminal' -c 'router ospf' "
            f"-c 'no network {prefix} area {area}' -c 'end'",
        )
        line = f"network {prefix} area {area}".replace(".", "\\.").replace("/", "\\/")
        self.runtime.exec(
            params.host_name,
            "sed -i -E "
            f"'{_OSPF_BLOCK_RANGE} s/^([[:space:]]+)({line})[[:space:]]*$/\\1# \\2/' "
            "/etc/frr/frr.conf",
        )
        self.logger.info(
            f"Injected OSPF neighbor missing on {params.host_name} "
            f"(removed network {prefix} area {area})."
        )

    def verify_fault(self, params: OSPFNeighborMissingParams) -> dict:
        """Verify one ``network`` statement is gone from the daemon and the others stay."""
        commented = re.findall(
            r"^\s*#\s*network\s+(\S+)\s+area\s+(\S+)",
            self.runtime.exec(
                params.host_name,
                f"sed -n -E '{_OSPF_BLOCK_RANGE} p' /etc/frr/frr.conf 2>/dev/null",
            ),
            re.MULTILINE,
        )
        running = ospf_networks(
            self.runtime.exec(
                params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
            )
        )
        dropped = [tuple(item) for item in commented]
        verified = len(dropped) == 1 and dropped[0] not in running and len(running) > 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "dropped_network": list(dropped[0]) if dropped else None,
                "running_network_count": len(running),
            },
        )
