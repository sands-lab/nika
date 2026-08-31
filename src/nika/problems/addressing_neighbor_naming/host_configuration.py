import re
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.support.inject_resolve import (
    derive_incorrect_ip,
    derive_wrong_gateway,
)
from nika.problems.rca import node_resource
from nika.problems.rca.inventory import interface_on
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger


def _read_host_ipv4(
    runtime,
    host_name: str,
    intf_name: str = "eth0",
) -> str | None:
    """Return host IPv4 CIDR on ``intf_name``, with exec fallback."""
    ip = runtime.get_host_ip(host_name, intf_name, with_prefix=True)
    if ip:
        return ip
    line = runtime.exec(
        host_name,
        f"ip -4 -o addr show dev {intf_name} scope global 2>/dev/null | head -1",
    ).strip()
    match = re.search(r"inet\s+(\S+)", line)
    return match.group(1) if match else None


def _inject_ip_change(
    runtime,
    *,
    host_name: str,
    old_ip: str,
    new_ip: str,
    intf_name: str,
    new_gateway: str | None = None,
) -> None:
    runtime.exec(host_name, f"ip addr del {old_ip} dev {intf_name}")
    runtime.exec(host_name, f"ip addr add {new_ip} dev {intf_name}")
    if new_gateway:
        runtime.exec(host_name, f"ip route add default via {new_gateway}")


# ==========================================
# Problem: Host missing IP address
# ==========================================


class HostMissingIPParams(BaseModel):
    """Parameters for injecting a params.host_name-missing-IP fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class HostMissingIP(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "host_missing_ip"
    description = "Host interface has no IP address."
    TAGS: str = ["pc"]

    Params = HostMissingIPParams

    symptom_desc = (
        "Some hosts are unable to communicate with other devices in the network."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self.intf_name = "eth0"

    def root_cause_resources(self, params: HostMissingIPParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: HostMissingIPParams):
        real_ip = self.runtime.get_host_ip(
            params.host_name, params.intf_name, with_prefix=True
        )
        real_gateway = self.runtime.get_default_gateway(params.host_name)
        self.runtime.exec(
            params.host_name, f"ip addr del {real_ip} dev {params.intf_name}"
        )
        self.runtime.exec(
            params.host_name, f"echo '{real_ip} {real_gateway}' > /tmp/removed_ip.txt"
        )
        self.logger.info(
            f"Injected missing IP on {params.host_name} from {real_ip} and gateway {real_gateway}."
        )

    def verify_fault(self, params: HostMissingIPParams) -> dict:
        """Verify that the params.host_name has no global IPv4 address on the interface."""
        ip_line = self.runtime.exec(
            params.host_name, f"ip -4 -o addr show dev {params.intf_name} scope global"
        ).strip()
        verified = "inet " not in ip_line
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": params.intf_name,
                "ip_line": ip_line,
            },
        )


# ==========================================
""" Problem: Host IP conflict """


class HostIPConflictParams(BaseModel):
    """Parameters for injecting a params.host_name IP conflict fault."""

    host_name: str = Field(description="Source params.host_name whose IP is copied.")
    host_name_2: str = Field(description="Target params.host_name to misconfigure.")


class HostIPConflict(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "host_ip_conflict"
    description = "Two hosts are configured with the same IP address."
    TAGS: str = ["pc"]

    Params = HostIPConflictParams

    symptom_desc = "Some hosts experience intermittent connectivity issues."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: HostIPConflictParams):
        return [interface_on(self.net_env, params.host_name_2, "eth0")]

    def inject_fault(self, params: HostIPConflictParams):
        src_host = params.host_name
        dst_host = params.host_name_2
        _inject_ip_change(
            self.runtime,
            host_name=dst_host,
            old_ip=self.runtime.get_host_ip(dst_host, "eth0", with_prefix=True),
            new_ip=self.runtime.get_host_ip(src_host, "eth0", with_prefix=True),
            intf_name="eth0",
            new_gateway=self.runtime.get_default_gateway(src_host),
        )

    def verify_fault(self, params: HostIPConflictParams) -> dict:
        """Verify both hosts share the same eth0 IP (conflict)."""
        host_a = params.host_name
        host_b = params.host_name_2
        cmd = "ip -4 -o addr show dev eth0 scope global | awk '/inet /{print $4}'"
        ip_a_raw = self.runtime.exec(host_a, cmd).strip()
        ip_b_raw = self.runtime.exec(host_b, cmd).strip()
        ip_a = ip_a_raw.split("/")[0] if ip_a_raw else ""
        ip_b = ip_b_raw.split("/")[0] if ip_b_raw else ""
        verified = bool(ip_a) and ip_a == ip_b
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host_a": host_a, "host_b": host_b, "ip_a": ip_a, "ip_b": ip_b},
        )


# ==========================================
# Problem: Incorrect Host IP
# ==========================================


class HostIncorrectIPParams(BaseModel):
    """Parameters for injecting an incorrect params.host_name IP fault."""

    host_name: str = Field(description="Target host name.")
    incorrect_ip: Optional[str] = Field(
        default=None,
        description="Incorrect CIDR IP. Derived at inject time if omitted.",
    )


class HostIncorrectIP(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "host_incorrect_ip"
    description = "Host IP address is incorrect."
    TAGS: str = ["pc"]

    Params = HostIncorrectIPParams

    symptom_desc = "Some hosts seem to be unreachable in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_ip: str | None = None

    def root_cause_resources(self, params: HostIncorrectIPParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: HostIncorrectIPParams):
        old_ip = _read_host_ipv4(self.runtime, params.host_name, "eth0")
        self._original_ip = old_ip
        if old_ip:
            self.runtime.exec(
                params.host_name,
                f"printf '%s\\n' '{old_ip.split('/')[0]}' > /tmp/nika_original_ip",
            )
        incorrect_ip = params.incorrect_ip or derive_incorrect_ip(
            self.runtime, params.host_name
        )
        _inject_ip_change(
            self.runtime,
            host_name=params.host_name,
            old_ip=old_ip,
            new_ip=incorrect_ip,
            intf_name="eth0",
            new_gateway=self.runtime.get_default_gateway(params.host_name),
        )

    def verify_fault(self, params: HostIncorrectIPParams) -> dict:
        """Verify that the params.host_name eth0 IP differs from the original address at inject time."""
        ip_line = self.runtime.exec(
            params.host_name, "ip -4 -o addr show dev eth0 scope global"
        ).strip()
        current_ip = None
        if "inet " in ip_line:
            parts = ip_line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    current_ip = parts[i + 1]
                    break
        original = self._original_ip
        if not original:
            stored = self.runtime.exec(
                params.host_name,
                "cat /tmp/nika_original_ip 2>/dev/null || true",
            ).strip()
            if stored:
                original = stored
        current_addr = current_ip.split("/")[0] if current_ip else None
        original_addr = original.split("/")[0] if original else None
        verified = (
            bool(current_addr) and bool(original_addr) and current_addr != original_addr
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "ip_line": ip_line,
                "original_ip": original,
                "current_ip": current_ip,
            },
        )


# ==========================================
# Problem: Incorrect Host Gateway
# ==========================================


class HostIncorrectGatewayParams(BaseModel):
    """Parameters for injecting an incorrect params.host_name gateway fault."""

    host_name: str = Field(description="Target host name.")
    new_gateway: Optional[str] = Field(
        default=None,
        description="Incorrect gateway IP. Derived at inject time if omitted.",
    )


class HostIncorrectGateway(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "host_incorrect_gateway"
    description = "Host default gateway is incorrect."
    TAGS: str = ["pc", "frr"]

    Params = HostIncorrectGatewayParams

    symptom_desc = "Some hosts seem to be unreachable in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._injected_gateway: str | None = None

    def root_cause_resources(self, params: HostIncorrectGatewayParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HostIncorrectGatewayParams):
        new_gateway = params.new_gateway or derive_wrong_gateway(
            self.runtime, params.host_name
        )
        self._injected_gateway = new_gateway
        _inject_ip_change(
            self.runtime,
            host_name=params.host_name,
            old_ip=self.runtime.get_host_ip(params.host_name, "eth0", with_prefix=True),
            new_ip=self.runtime.get_host_ip(params.host_name, "eth0", with_prefix=True),
            intf_name="eth0",
            new_gateway=new_gateway,
        )

    def verify_fault(self, params: HostIncorrectGatewayParams) -> dict:
        """Verify that the default route uses the injected wrong gateway."""
        route_line = self.runtime.exec(
            params.host_name, "ip route show default"
        ).strip()
        expected_gateway = params.new_gateway or self._injected_gateway
        if not expected_gateway:
            # Fresh instance after workflow inject: .254 derivation is idempotent
            # once the wrong gateway is already installed.
            try:
                expected_gateway = derive_wrong_gateway(self.runtime, params.host_name)
            except ValueError:
                expected_gateway = None
        verified = bool(expected_gateway) and expected_gateway in route_line
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "route_line": route_line,
                "expected_gateway": expected_gateway,
            },
        )


# ==========================================
# Problem: Incorrect Host netmask
# ==========================================


class HostIncorrectNetmaskParams(BaseModel):
    """Parameters for injecting an incorrect params.host_name netmask fault."""

    host_name: str = Field(description="Target host name.")
    netmask_prefix: int = Field(default=8, description="Incorrect prefix length.")


class HostIncorrectNetmask(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "host_incorrect_netmask"
    description = "Host netmask/prefix length is incorrect."
    TAGS: str = ["pc", "frr"]

    Params = HostIncorrectNetmaskParams

    symptom_desc = "Some hosts seem to be unreachable in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.netmask_prefix = 8

    def root_cause_resources(self, params: HostIncorrectNetmaskParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: HostIncorrectNetmaskParams):
        old_ip = self.runtime.get_host_ip(params.host_name, "eth0", with_prefix=True)
        ip_part = old_ip.split("/")[0]
        new_ip = f"{ip_part}/{params.netmask_prefix}"
        _inject_ip_change(
            self.runtime,
            host_name=params.host_name,
            old_ip=old_ip,
            new_ip=new_ip,
            intf_name="eth0",
            new_gateway=self.runtime.get_default_gateway(params.host_name),
        )

    def verify_fault(self, params: HostIncorrectNetmaskParams) -> dict:
        """Verify that eth0 has a non-/24 prefix (injected wrong netmask)."""
        ip_line = self.runtime.exec(
            params.host_name, "ip -4 -o addr show dev eth0 scope global"
        ).strip()
        prefix = None
        if "inet " in ip_line:
            parts = ip_line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    cidr = parts[i + 1]
                    if "/" in cidr:
                        prefix = int(cidr.split("/")[1])
                    break
        verified = prefix is not None and prefix != 24
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "ip_line": ip_line,
                "expected_prefix": params.netmask_prefix,
                "actual_prefix": prefix,
            },
        )


# ==========================================
# Problem: Incorrect Host DNS resolvers
# =========================================


class HostIncorrectDNSParams(BaseModel):
    """Parameters for injecting an incorrect DNS resolver fault."""

    host_name: str = Field(description="Target host name.")
    fake_dns_ip: str = Field(
        default="192.0.2.1",
        description="Incorrect DNS IP (TEST-NET; non-resolving).",
    )


class HostIncorrectDNS(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "host_incorrect_dns"
    description = "Host is configured with an incorrect DNS resolver."
    TAGS: str = ["dns"]

    Params = HostIncorrectDNSParams

    symptom_desc = "Some hosts are unable to access web services."

    def root_cause_resources(self, params: HostIncorrectDNSParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HostIncorrectDNSParams):
        self.runtime.exec(
            params.host_name,
            f"echo 'nameserver {params.fake_dns_ip}' > /etc/resolv.conf; "
            # Drop static hosts overrides so name lookups must use DNS.
            "sed -i -E '/\\sweb0\\.(local|pod0)\\s*$/d' /etc/hosts 2>/dev/null || true; "
            "sed -i -E '/\\swebserver/d' /etc/hosts 2>/dev/null || true",
        )

    def verify_fault(self, params: HostIncorrectDNSParams) -> dict:
        """Verify the incorrect-DNS fault by checking /etc/resolv.conf contains the fake DNS IP."""
        resolv = self.runtime.exec(
            params.host_name, "cat /etc/resolv.conf 2>/dev/null || echo ''"
        )
        verified = params.fake_dns_ip in resolv
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "fake_dns_ip": params.fake_dns_ip,
                "resolv_conf": resolv.strip(),
            },
        )
