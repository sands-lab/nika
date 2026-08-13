import ipaddress

from pydantic import BaseModel, Field

from nika.problems.inject_resolve import (
    resolve_victim_host,
    resolve_victim_host_ip,
)
from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==================================================================
""" Problem: BGP ASN misconfiguration. """
# ==================================================================


class BGPAsnMisconfigParams(BaseModel):
    """Parameters for injecting a BGP ASN misconfiguration fault."""

    host_name: str = Field(description="Target router host name.")


class BGPAsnMisconfig(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "bgp_asn_misconfig"
    TAGS: str = ["bgp"]

    Params = BGPAsnMisconfigParams

    symptom_desc = "Some hosts are experiencing connectivity issues."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPAsnMisconfigParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: BGPAsnMisconfigParams):
        match self.lab_backend:
            case "containerlab":
                self._inject_asn_misconfig_containerlab(params)
            case "kathara":
                self._inject_asn_misconfig_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_asn_misconfig_containerlab(self, params: BGPAsnMisconfigParams) -> None:
        as_number = self.runtime.srl_get_bgp_as(params.host_name)
        wrong_asn = as_number + 600
        self.runtime.srl_set_bgp_as(params.host_name, wrong_asn)
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} "
            f"from ASN {as_number} to {wrong_asn} (SRL)."
        )

    def _inject_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> None:
        as_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
        wrong_asn = as_number + 600
        self.runtime.exec(
            params.host_name,
            f"sed -i.bak 's/^router bgp {as_number}$/router bgp {wrong_asn}/' /etc/frr/frr.conf && service frr restart 2>/dev/null || true",
        )
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} from ASN {as_number} to {wrong_asn}."
        )

    def verify_fault(self, params: BGPAsnMisconfigParams) -> dict:
        """Verify the ASN in frr.conf or SRL running config was changed."""
        match self.lab_backend:
            case "containerlab":
                return self._verify_asn_misconfig_containerlab(params)
            case "kathara":
                return self._verify_asn_misconfig_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_asn_misconfig_containerlab(self, params: BGPAsnMisconfigParams) -> dict:
        running_asn = self.runtime.srl_get_bgp_as(params.host_name)
        orig_asn = getattr(self, "_orig_asn", None)
        wrong_asn = getattr(self, "_wrong_asn", None)
        verified = (wrong_asn is not None and running_asn == wrong_asn) or (
            orig_asn is not None and running_asn != orig_asn
        )
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "host": params.host_name,
                "orig_asn": orig_asn,
                "running_asn": running_asn,
                "wrong_asn": wrong_asn,
            },
        )

    def _verify_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> dict:
        file_asn_raw = self.runtime.exec(
            params.host_name,
            "grep -E '^router bgp' /etc/frr/frr.conf 2>/dev/null | awk '{print $3}'",
        ).strip()
        orig_asn_raw = self.runtime.exec(
            params.host_name,
            "grep -E '^router bgp' /etc/frr/frr.conf.bak 2>/dev/null | awk '{print $3}'",
        ).strip()
        running_asn_raw = self.runtime.exec(
            params.host_name,
            "vtysh -c 'show running-config' 2>/dev/null | grep -E '^router bgp' | awk '{print $3}'",
        ).strip()
        file_changed = (
            bool(file_asn_raw) and bool(orig_asn_raw) and file_asn_raw != orig_asn_raw
        )
        daemon_changed = bool(running_asn_raw) and running_asn_raw != orig_asn_raw
        verified = file_changed and daemon_changed
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "host": params.host_name,
                "file_asn": file_asn_raw,
                "orig_asn": orig_asn_raw,
                "running_asn": running_asn_raw,
            },
        )


# ==================================================================
""" Problem: BGP missing route advertisement. """
# ==================================================================


class BGPMissingAdvertiseParams(BaseModel):
    """Parameters for injecting a BGP missing route advertisement fault."""

    host_name: str = Field(description="Target router host name.")


class BGPMissingAdvertise(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "bgp_missing_route_advertisement"
    TAGS: str = ["bgp"]

    Params = BGPMissingAdvertiseParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPMissingAdvertiseParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: BGPMissingAdvertiseParams):
        match self.lab_backend:
            case "containerlab":
                self._inject_missing_adv_containerlab(params)
            case "kathara":
                self._inject_missing_adv_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_missing_adv_containerlab(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        prefix = str(
            ipaddress.ip_network(
                resolve_victim_host_ip(self.runtime, params.host_name),
                strict=False,
            )
        )
        self._withdrawn_prefix = prefix
        self.runtime.srl_withdraw_bgp_prefix(params.host_name, prefix)
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(SRL export-policy block for {prefix})."
        )

    def _inject_missing_adv_kathara(self, params: BGPMissingAdvertiseParams) -> None:
        self.runtime.exec(
            params.host_name,
            "sed -i.bak -E 's/^([[:space:]]*)network /\\1# network /' /etc/frr/frr.conf && service frr restart 2>/dev/null || true",
        )
        self.logger.info(f"Injected BGP missing route on {params.host_name}.")

    def verify_fault(self, params: BGPMissingAdvertiseParams) -> dict:
        """Verify route withdrawal in frr.conf or SRL BGP export-policy."""
        match self.lab_backend:
            case "containerlab":
                return self._verify_missing_adv_containerlab(params)
            case "kathara":
                return self._verify_missing_adv_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_missing_adv_containerlab(
        self, params: BGPMissingAdvertiseParams
    ) -> dict:
        prefix = getattr(
            self,
            "_withdrawn_prefix",
            str(
                ipaddress.ip_network(
                    resolve_victim_host_ip(self.runtime, params.host_name),
                    strict=False,
                )
            ),
        )
        verified = self.runtime.srl_bgp_prefix_withdrawn(params.host_name, prefix)
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name, "prefix": prefix},
        )

    def _verify_missing_adv_kathara(self, params: BGPMissingAdvertiseParams) -> dict:
        count_raw = self.runtime.exec(
            params.host_name,
            "grep -c '^[[:space:]]*# network' /etc/frr/frr.conf 2>/dev/null || echo 0",
        ).strip()
        try:
            count = int(count_raw)
        except ValueError:
            count = 0
        running_count_raw = self.runtime.exec(
            params.host_name,
            "vtysh -c 'show running-config' 2>/dev/null | grep -c '^[[:space:]]*network' || echo 0",
        ).strip()
        try:
            running_count = int(running_count_raw)
        except ValueError:
            running_count = 0
        verified = count > 0 and running_count == 0
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "host": params.host_name,
                "commented_network_count": count,
                "running_network_count": running_count,
            },
        )


# ==================================================================
""" Problem: BGP static blackhole route misconfiguration problem. """
# ==================================================================


class StaticBlackHoleParams(BaseModel):
    """Parameters for injecting a static blackhole route fault."""

    host_name: str = Field(description="Target router host name.")


class StaticBlackHole(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "host_static_blackhole"
    TAGS: str = ["bgp"]

    Params = StaticBlackHoleParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: StaticBlackHoleParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: StaticBlackHoleParams):
        self.victim_device = resolve_victim_host(self.runtime, params.host_name)
        host_network = ipaddress.ip_network(
            resolve_victim_host_ip(self.runtime, params.host_name),
            strict=False,
        )
        self._blackhole_network = str(host_network)
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_add_blackhole_static(
                    params.host_name, self._blackhole_network
                )
            case "kathara":
                self.runtime.exec(
                    params.host_name, f"ip route replace blackhole {host_network}"
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )
        self.logger.info(
            f"Injected addition of blackhole route {host_network} on {params.host_name}."
        )

    def verify_fault(self, params: StaticBlackHoleParams) -> dict:
        """Verify a blackhole route for the victim's network exists."""
        host_network = str(
            ipaddress.ip_network(
                resolve_victim_host_ip(self.runtime, params.host_name),
                strict=False,
            )
        )
        match self.lab_backend:
            case "containerlab":
                verified = self.runtime.srl_blackhole_static_present(
                    params.host_name, host_network
                )
                return build_verify_result(
                    root_cause_name=self.root_cause_name,
                    faulty_devices=self.faulty_devices,
                    verified=verified,
                    details={"host": params.host_name, "network": host_network},
                )
            case "kathara":
                route_output = self.runtime.exec(
                    params.host_name, "ip route show"
                ).strip()
                verified = (
                    f"blackhole {host_network}" in route_output
                    or "blackhole" in route_output
                )
                return build_verify_result(
                    root_cause_name=self.root_cause_name,
                    faulty_devices=self.faulty_devices,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "network": host_network,
                        "route_output": route_output,
                    },
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )


# ==================================================================
""" Problem: BGP blackhole route advertisement misconfiguration problem. """
# ==================================================================


class BGPBlackholeRouteLeakParams(BaseModel):
    """Parameters for injecting a BGP blackhole route leak fault."""

    host_name: str = Field(description="Target router host name.")


class BGPBlackholeRouteLeak(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "bgp_blackhole_route_leak"
    TAGS: str = ["bgp"]

    Params = BGPBlackholeRouteLeakParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPBlackholeRouteLeakParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: BGPBlackholeRouteLeakParams):
        self.victim_device = resolve_victim_host(self.runtime, params.host_name)
        victim_ip = resolve_victim_host_ip(
            self.runtime, params.host_name, with_prefix=False
        )
        network_30 = ipaddress.ip_network(f"{victim_ip}/30", strict=False)
        self._leak_network = str(network_30)
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_add_blackhole_route_leak(
                    params.host_name, self._leak_network
                )
                self.logger.info(
                    f"Injected BGP advertise blackhole route on {params.host_name}: "
                    f"{network_30} (SRL)."
                )
            case "kathara":
                as_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
                cmd = (
                    "vtysh -c 'configure terminal' "
                    f"-c 'ip route {network_30} Null0' "
                    f"-c 'router bgp {as_number}' "
                    f"-c 'network {network_30}' "
                    "-c 'end' "
                    "-c 'write memory' "
                )
                self.runtime.exec(params.host_name, cmd)
                self.logger.info(
                    f"Injected BGP advertise blackhole route on {params.host_name}: {network_30}."
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def verify_fault(self, params: BGPBlackholeRouteLeakParams) -> dict:
        """Verify blackhole route leak in running config."""
        victim_ip = resolve_victim_host_ip(
            self.runtime, params.host_name, with_prefix=False
        )
        network_30 = str(ipaddress.ip_network(f"{victim_ip}/30", strict=False))
        match self.lab_backend:
            case "containerlab":
                has_blackhole = self.runtime.srl_blackhole_static_present(
                    params.host_name, network_30
                )
                has_advertise = self.runtime.srl_prefix_advertised(
                    params.host_name, network_30
                )
                verified = has_blackhole or has_advertise
                return build_verify_result(
                    root_cause_name=self.root_cause_name,
                    faulty_devices=self.faulty_devices,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "network_30": network_30,
                        "has_blackhole": has_blackhole,
                        "has_advertise": has_advertise,
                    },
                )
            case "kathara":
                running_config = self.runtime.exec(
                    params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
                ).strip()
                has_null_route = (
                    f"ip route {network_30} Null0" in running_config
                    or "Null0" in running_config
                )
                return build_verify_result(
                    root_cause_name=self.root_cause_name,
                    faulty_devices=self.faulty_devices,
                    verified=has_null_route,
                    details={
                        "host": params.host_name,
                        "network_30": network_30,
                        "has_null_route": has_null_route,
                    },
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )
