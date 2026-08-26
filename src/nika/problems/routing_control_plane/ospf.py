import re

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==================================================================
# Problem: OSPF Area Misconfiguration
# ==================================================================


class OSPFAreaMisconfigParams(BaseModel):
    """Parameters for injecting an OSPF area misconfiguration fault."""

    host_name: str = Field(description="Target router host name.")


class OSPFAreaMisconfig(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "ospf_area_misconfiguration"
    effect_protocol = "ospf"

    TAGS: str = ["ospf"]

    Params = OSPFAreaMisconfigParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFAreaMisconfigParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: OSPFAreaMisconfigParams):
        running_cfg = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config'"
        )
        pattern = re.compile(r"^\s*network\s+\S+\s+area\s+(\S+)", re.MULTILINE)
        m = pattern.search(running_cfg)
        if not m:
            self.logger.error(f"Could not find OSPF area on {params.host_name}")
        correct_area = m.group(1)
        wrong_area = "66" if correct_area != "66" else "99"
        self.runtime.exec(
            params.host_name,
            f"sed -i.bak -E 's/(area ){correct_area}$/\\1{wrong_area}/g' /etc/frr/frr.conf && service frr restart 2>/dev/null || true",
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
        orig_areas_raw = self.runtime.exec(
            params.host_name,
            "grep -E '^[[:space:]]*network .* area ' /etc/frr/frr.conf.bak 2>/dev/null | awk '{print $NF}' | sort -u",
        ).strip()
        running_areas_raw = self.runtime.exec(
            params.host_name,
            "vtysh -c 'show running-config' 2>/dev/null | grep -E '^[[:space:]]*network .* area ' | awk '{print $NF}' | sort -u",
        ).strip()
        file_areas = set(file_areas_raw.splitlines()) if file_areas_raw else set()
        orig_areas = set(orig_areas_raw.splitlines()) if orig_areas_raw else set()
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


class OSPFNeighborMissingParams(BaseModel):
    """Parameters for injecting an OSPF neighbor missing fault."""

    host_name: str = Field(description="Target router host name.")


class OSPFNeighborMissing(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "ospf_neighbor_missing"
    effect_protocol = "ospf"

    TAGS: str = ["ospf"]

    Params = OSPFNeighborMissingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: OSPFNeighborMissingParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: OSPFNeighborMissingParams):
        cmd = (
            "sed -i.bak -E "
            "'s|^([[:space:]]*)network([[:space:]])|\\1# network\\2|' "
            "/etc/frr/frr.conf"
        )
        self.runtime.exec(params.host_name, cmd)
        self.runtime.exec(params.host_name, "service frr restart 2>/dev/null || true")
        self.logger.info(f"Injected OSPF neighbor missing on {params.host_name}.")

    def verify_fault(self, params: OSPFNeighborMissingParams) -> dict:
        """Verify network lines are commented in frr.conf and removed from the running daemon."""
        commented_count_raw = self.runtime.exec(
            params.host_name,
            "grep -c '^[[:space:]]*# network' /etc/frr/frr.conf 2>/dev/null || echo 0",
        ).strip()
        try:
            commented_count = int(commented_count_raw)
        except ValueError:
            commented_count = 0
        running_network_count_raw = self.runtime.exec(
            params.host_name,
            "vtysh -c 'show running-config' 2>/dev/null | grep -c '^[[:space:]]*network' || echo 0",
        ).strip()
        try:
            running_network_count = int(running_network_count_raw)
        except ValueError:
            running_network_count = 0
        verified = commented_count > 0 and running_network_count == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "commented_network_count": commented_count,
                "running_network_count": running_network_count,
            },
        )
