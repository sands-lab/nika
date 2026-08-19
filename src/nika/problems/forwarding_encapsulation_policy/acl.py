from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    build_verify_result,
    ProblemBase,
)
from nika.runtime.base import RuntimeCapabilityError

# ==================================================================
# Problem: BGP Access Policy Misconfiguration - ACL blocking BGP traffic
# ==================================================================


class BGPAclBlockParams(BaseModel):
    """Parameters for injecting a BGP ACL block fault."""

    host_name: str = Field(description="Target router host name.")


class BGPAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "bgp_acl_block"
    TAGS: str = ["bgp"]

    Params = BGPAclBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: BGPAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: BGPAclBlockParams):
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_add_bgp_acl_drop_179(params.host_name)
            case "kathara":
                self.runtime.add_nft_drop_rule(
                    params.host_name, "tcp dport 179 drop", family="inet"
                )
                self.runtime.add_nft_drop_rule(
                    params.host_name, "tcp sport 179 drop", family="inet"
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def verify_fault(self, params: BGPAclBlockParams) -> dict:
        """Verify nftables or SRL ACL blocks TCP port 179 (BGP)."""
        match self.lab_backend:
            case "containerlab":
                verified = self.runtime.srl_bgp_acl_drop_179_present(params.host_name)
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={"host": params.host_name, "srl_acl": verified},
                )
            case "kathara":
                nft_output = self.runtime.exec(
                    params.host_name, "nft list ruleset 2>/dev/null"
                ).strip()
                verified = "tcp dport 179" in nft_output and "drop" in nft_output
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={"host": params.host_name, "nft_snippet": nft_output},
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )


# ==================================================================
# Problem: OSPF Access Policy Misconfiguration - ACL blocking OSPF traffic
# ==================================================================


class OSPFAclBlockParams(BaseModel):
    """Parameters for injecting an OSPF ACL block fault."""

    host_name: str = Field(description="Target router host name.")


class OSPFAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "ospf_acl_block"
    TAGS: str = ["ospf"]

    Params = OSPFAclBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: OSPFAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: OSPFAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "ip protocol ospf drop", family="inet"
        )
        self.runtime.add_nft_drop_rule(
            params.host_name, "ip protocol ospf drop", family="inet"
        )

    def verify_fault(self, params: OSPFAclBlockParams) -> dict:
        """Verify nftables has a rule blocking OSPF protocol."""
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = "ospf" in nft_output and "drop" in nft_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_snippet": nft_output},
        )


# ==================================================================
# Problem: ARP Access Policy Misconfiguration - ACL blocking ARP traffic
# ==================================================================


class ARPAclBlockParams(BaseModel):
    """Parameters for injecting an ARP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class ARPAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.LINK
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "arp_acl_block"
    TAGS: str = ["arp"]

    Params = ARPAclBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: ARPAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: ARPAclBlockParams):
        self.runtime.add_nft_drop_rule(params.host_name, "drop", family="arp")
        self.runtime.exec(params.host_name, "ip neigh flush all")

    def verify_fault(self, params: ARPAclBlockParams) -> dict:
        """Verify nftables has a rule blocking ARP traffic."""
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = "arp" in nft_output and "drop" in nft_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_snippet": nft_output},
        )


# ==================================================================
# Problem: ACL blocking ICMP traffic
# ==================================================================


class IcmpAclBlockParams(BaseModel):
    """Parameters for injecting an ICMP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class IcmpAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "icmp_acl_block"
    TAGS: str = ["icmp"]

    Params = IcmpAclBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: IcmpAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: IcmpAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "ip protocol icmp drop", family="ip"
        )

    def verify_fault(self, params: IcmpAclBlockParams) -> dict:
        """Verify nftables has a rule blocking ICMP traffic."""
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = "icmp" in nft_output and "drop" in nft_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_snippet": nft_output},
        )


# ==================================================================
# Problem: ACL blocking HTTP traffic
# ==================================================================


class HttpAclBlockParams(BaseModel):
    """Parameters for injecting an HTTP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class HttpAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.SERVICE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "http_acl_block"
    TAGS: str = ["http", "pc"]

    Params = HttpAclBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: HttpAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HttpAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "tcp dport 80 drop", family="inet"
        )

    def verify_fault(self, params: HttpAclBlockParams) -> dict:
        """Verify nftables has a rule blocking HTTP (port 80) traffic."""
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = "tcp dport 80" in nft_output and "drop" in nft_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_snippet": nft_output},
        )


# ==================================================================
# Problem: DNS listener port blocked
# ==================================================================


class DNSPortBlockedParams(BaseModel):
    """Parameters for injecting a DNS port blocked fault."""

    host_name: str = Field(description="Target DNS server host name.")


class DNSPortBlocked(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.SERVICE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "dns_port_blocked"

    TAGS: str = ["dns", "http"]

    Params = DNSPortBlockedParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DNSPortBlockedParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DNSPortBlockedParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "tcp dport 53 drop", family="inet"
        )
        self.runtime.add_nft_drop_rule(
            params.host_name, "udp dport 53 drop", family="inet"
        )

    def verify_fault(self, params: DNSPortBlockedParams) -> dict:
        """Verify nftables has rules blocking DNS port 53."""
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = "dport 53" in nft_output and "drop" in nft_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_snippet": nft_output},
        )
