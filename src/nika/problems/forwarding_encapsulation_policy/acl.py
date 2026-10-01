from pydantic import BaseModel, Field

from nika.net_env.verify import frr_bgp_has_established_session
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.polling import wait_until
from nika.runtime.base import RuntimeCapabilityError


def _verify_nft_drop(
    problem: ProblemBase, host_name: str, *rules: str, family: str = "inet"
) -> dict:
    """Verify every ``add_nft_drop_rule`` rule is live, matched exactly."""
    present = {
        rule: problem.runtime.nft_drop_rule_present(host_name, rule, family=family)
        for rule in rules
    }
    return build_verify_result(
        fault_type=problem.root_cause_name,
        verified=all(present.values()),
        details={"host": host_name, "family": family, "rules_present": present},
    )


# ==================================================================
# Problem: BGP Access Policy Misconfiguration - ACL blocking BGP traffic
# ==================================================================


class BGPAclBlockParams(BaseModel):
    """Parameters for injecting a BGP ACL block fault."""

    host_name: str = Field(description="Target router host name.")


class BGPAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "bgp_acl_block"
    description = "BGP control-plane traffic is blocked by an ACL."
    TAGS: str = ["bgp"]
    supported_backends = ("kathara", "containerlab")

    Params = BGPAclBlockParams

    def root_cause_resources(self, params: BGPAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: BGPAclBlockParams):
        match self.lab_backend:
            case "containerlab":
                self._srl_acl_entries = self.runtime.srl_add_bgp_acl_drop_179(
                    params.host_name
                )
            case "kathara":
                self.runtime.add_nft_drop_rule(
                    params.host_name, "tcp dport 179 drop", family="inet"
                )
                self.runtime.add_nft_drop_rule(
                    params.host_name, "tcp sport 179 drop", family="inet"
                )
                # Hard-reset every session so the ACL takes effect now instead
                # of after the 180 s hold timer; blocked TCP 179 keeps them down.
                self.runtime.exec(
                    params.host_name,
                    "vtysh -c 'clear ip bgp *' 2>/dev/null || true",
                )
                wait_until(
                    lambda: (
                        not frr_bgp_has_established_session(
                            self.runtime, params.host_name
                        )
                    ),
                    8,
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def verify_fault(self, params: BGPAclBlockParams) -> dict:
        """Verify nftables or SRL ACL blocks TCP port 179 (BGP)."""
        match self.lab_backend:
            case "containerlab":
                entries = getattr(self, "_srl_acl_entries", {})
                verified = self.runtime.srl_bgp_acl_drop_179_present(
                    params.host_name, entries
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "srl_acl": verified,
                        "srl_acl_entries": sorted(entries),
                    },
                )
            case "kathara":
                return _verify_nft_drop(
                    self, params.host_name, "tcp dport 179 drop", "tcp sport 179 drop"
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
    root_cause_name = "ospf_acl_block"
    description = "OSPF control-plane traffic is blocked by an ACL."
    TAGS: str = ["ospf"]

    Params = OSPFAclBlockParams

    def root_cause_resources(self, params: OSPFAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: OSPFAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "ip protocol ospf drop", family="inet"
        )

    def verify_fault(self, params: OSPFAclBlockParams) -> dict:
        """Verify nftables has a rule blocking OSPF protocol."""
        return _verify_nft_drop(self, params.host_name, "ip protocol ospf drop")


# ==================================================================
# Problem: ARP Access Policy Misconfiguration - ACL blocking ARP traffic
# ==================================================================


class ARPAclBlockParams(BaseModel):
    """Parameters for injecting an ARP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class ARPAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "arp_acl_block"
    description = "ARP is blocked by an ACL."
    TAGS: str = ["arp"]
    # k8s_lab / llmd_lab client images lack nftables and cannot apt-install offline.
    COMPATIBLE_COLUMNS = frozenset(
        {
            "campus_lan",
            "dc_clos",
            "enterprise_branch",
            "sdn_l3_clos",
            "p4_dc_fabric",
            "p4_dc_gateway",
            "iosxr_simple_bgp",
        }
    )

    Params = ARPAclBlockParams

    def root_cause_resources(self, params: ARPAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: ARPAclBlockParams):
        self.runtime.add_nft_drop_rule(params.host_name, "drop", family="arp")
        # Clos/P4 startups install nud permanent GW neigh; flush all leaves those.
        self.runtime.exec(params.host_name, "ip neigh flush nud permanent")
        self.runtime.exec(params.host_name, "ip neigh flush all")

    def verify_fault(self, params: ARPAclBlockParams) -> dict:
        """Verify nftables has a rule blocking ARP traffic."""
        return _verify_nft_drop(self, params.host_name, "drop", family="arp")


# ==================================================================
# Problem: ACL blocking ICMP traffic
# ==================================================================


class IcmpAclBlockParams(BaseModel):
    """Parameters for injecting an ICMP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class IcmpAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "icmp_acl_block"
    description = "ICMP is blocked by an ACL."
    TAGS: str = ["icmp"]

    Params = IcmpAclBlockParams

    def root_cause_resources(self, params: IcmpAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: IcmpAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "ip protocol icmp drop", family="ip"
        )

    def verify_fault(self, params: IcmpAclBlockParams) -> dict:
        """Verify nftables has a rule blocking ICMP traffic."""
        return _verify_nft_drop(
            self, params.host_name, "ip protocol icmp drop", family="ip"
        )


# ==================================================================
# Problem: ACL blocking HTTP traffic
# ==================================================================


class HttpAclBlockParams(BaseModel):
    """Parameters for injecting an HTTP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class HttpAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "http_acl_block"
    description = "HTTP traffic is blocked by an ACL."
    TAGS: str = ["http", "pc"]

    Params = HttpAclBlockParams

    def root_cause_resources(self, params: HttpAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HttpAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "tcp dport 80 drop", family="inet"
        )

    def verify_fault(self, params: HttpAclBlockParams) -> dict:
        """Verify nftables has a rule blocking HTTP (port 80) traffic."""
        return _verify_nft_drop(self, params.host_name, "tcp dport 80 drop")


# ==================================================================
# Problem: DNS listener port blocked
# ==================================================================


class DNSPortBlockedParams(BaseModel):
    """Parameters for injecting a DNS port blocked fault."""

    host_name: str = Field(description="Target DNS server host name.")


class DNSPortBlocked(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "dns_port_blocked"

    description = "DNS service port is blocked."
    TAGS: str = ["dns", "http"]

    Params = DNSPortBlockedParams

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
        return _verify_nft_drop(
            self, params.host_name, "tcp dport 53 drop", "udp dport 53 drop"
        )
