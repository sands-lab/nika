from pydantic import BaseModel, Field

from nika.problems.topology_inventory import interface_on
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==================================================================
# Problem: MAC address conflict
# ==================================================================


class MacAddressConflictParams(BaseModel):
    """Parameters for injecting a MAC address conflict fault."""

    host_name: str = Field(description="Target host/device receiving conflicting MAC.")
    host_name_2: str = Field(description="Peer device whose MAC is copied.")


class MacAddressConflict(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "mac_address_conflict"
    TAGS: str = ["mac"]

    Params = MacAddressConflictParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: MacAddressConflictParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: MacAddressConflictParams):
        device_0 = params.host_name
        device_1 = params.host_name_2
        target_mac = self.runtime.get_host_mac_address(device_1, "eth0")
        self.runtime.exec(device_0, f"ip link set dev eth0 address {target_mac}")
        self.logger.info(
            f"Injected MAC address conflict on {device_0} with MAC {target_mac} of {device_1}"
        )

    def verify_fault(self, params: MacAddressConflictParams) -> dict:
        """Verify device_0's eth0 MAC matches device_1's eth0 MAC (conflict)."""
        device_0 = params.host_name
        device_1 = params.host_name_2
        mac_0 = self.runtime.get_host_mac_address(device_0, "eth0")
        mac_1 = self.runtime.get_host_mac_address(device_1, "eth0")
        verified = bool(mac_0) and mac_0.lower() == mac_1.lower()
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "device_0": device_0,
                "device_1": device_1,
                "mac_0": mac_0,
                "mac_1": mac_1,
            },
        )
