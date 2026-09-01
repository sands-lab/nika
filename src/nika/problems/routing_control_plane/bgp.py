import ipaddress
import re
import time

from pydantic import BaseModel, Field

from nika.problems.support.inject_resolve import resolve_victim_host_ip
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
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
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "bgp_asn_misconfig"
    description = "BGP local ASN is misconfigured relative to peer expectation."
    effect_protocol = "bgp"
    TAGS: str = ["bgp"]
    supported_backends = ("kathara", "containerlab")

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
        self.runtime.exec(
            params.host_name,
            f"printf '%s\\n' '{as_number}' > /tmp/nika_orig_bgp_asn",
        )
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} "
            f"from ASN {as_number} to {wrong_asn} (SRL)."
        )

    def _inject_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> None:
        as_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
        wrong_asn = as_number + 600
        # k8s_lab uses split FRR configs (/etc/frr/bgpd.conf); campus etc. use frr.conf.
        # Patch on-disk configs and bounce daemons so split-config labs reload.
        # Avoid shell "$vars" inside runtime.exec wrapping (quote escaping breaks loops).
        patched: list[str] = []
        for conf in ("/etc/frr/frr.conf", "/etc/frr/bgpd.conf"):
            exists = self.runtime.exec(
                params.host_name,
                f"test -f {conf} && echo yes || echo no",
                timeout=10,
            ).strip()
            if exists != "yes":
                continue
            self.runtime.exec(
                params.host_name,
                f"cp -a {conf} {conf}.bak",
                timeout=10,
            )
            self.runtime.exec(
                params.host_name,
                f"sed -i -E 's/^router bgp [0-9]+/router bgp {wrong_asn}/' {conf}",
                timeout=10,
            )
            patched.append(conf)
        verify_files = self.runtime.exec(
            params.host_name,
            "grep -E '^router bgp' /etc/frr/frr.conf /etc/frr/bgpd.conf 2>/dev/null || true",
            timeout=10,
        )
        if not patched or f"router bgp {wrong_asn}" not in verify_files:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: could not patch BGP ASN files on "
                f"{params.host_name!r}: patched={patched!r} grep={verify_files!r}"
            )
        self.runtime.exec(
            params.host_name,
            (
                "pkill -9 -x bgpd 2>/dev/null || true; "
                "pkill -9 -x zebra 2>/dev/null || true; "
                "sleep 1; "
                "/usr/lib/frr/frrinit.sh start 2>/dev/null || "
                "systemctl start frr 2>/dev/null || "
                "service frr start 2>/dev/null || "
                "("
                "  [ -x /usr/lib/frr/zebra ] && /usr/lib/frr/zebra -d -A 127.0.0.1; "
                "  [ -x /usr/lib/frr/bgpd ] && /usr/lib/frr/bgpd -d -A 127.0.0.1; "
                ")"
            ),
            timeout=90,
        )
        time.sleep(12)
        running = self.runtime.frr_get_bgp_asn_number(params.host_name)
        if running != wrong_asn:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: FRR on {params.host_name!r} "
                f"did not apply ASN change ({as_number} -> {wrong_asn}); "
                f"running={running}."
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
        if orig_asn is None:
            stored = self.runtime.exec(
                params.host_name,
                "cat /tmp/nika_orig_bgp_asn 2>/dev/null || true",
            ).strip()
            if stored.isdigit():
                orig_asn = int(stored)
                wrong_asn = orig_asn + 600
        verified = (wrong_asn is not None and running_asn == wrong_asn) or (
            orig_asn is not None and running_asn != orig_asn
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "orig_asn": orig_asn,
                "running_asn": running_asn,
                "wrong_asn": wrong_asn,
            },
        )

    def _verify_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> dict:
        orig_asn = getattr(self, "_orig_asn", None)
        expected_wrong = getattr(self, "_wrong_asn", None)
        file_asn_raw = self.runtime.exec(
            params.host_name,
            (
                "grep -E '^router bgp' /etc/frr/frr.conf /etc/frr/bgpd.conf "
                "2>/dev/null | awk '{print $NF}' | head -1"
            ),
        ).strip()
        orig_asn_raw = self.runtime.exec(
            params.host_name,
            (
                "grep -E '^router bgp' /etc/frr/frr.conf.bak /etc/frr/bgpd.conf.bak "
                "2>/dev/null | awk '{print $NF}' | head -1"
            ),
        ).strip()
        if not orig_asn_raw and orig_asn is not None:
            orig_asn_raw = str(orig_asn)
        try:
            running_asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
            running_asn_raw = str(running_asn)
        except Exception:
            running_asn = None
            running_asn_raw = ""
        verified = expected_wrong is not None and running_asn == expected_wrong
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "file_asn": file_asn_raw,
                "orig_asn": orig_asn_raw,
                "running_asn": running_asn_raw,
                "wrong_asn": expected_wrong,
            },
        )


# ==================================================================
""" Problem: BGP missing route advertisement. """
# ==================================================================


class BGPMissingAdvertiseParams(BaseModel):
    """Parameters for injecting a BGP missing route advertisement fault."""

    host_name: str = Field(description="Target router host name.")
    prefix: str | None = Field(
        default=None,
        description="Optional BGP prefix to withdraw (ISP originators).",
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional probe source host for symptom checks.",
    )
    probe_dst_ip: str | None = Field(
        default=None,
        description="Optional probe destination IP for symptom checks.",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer/stub host near the victim originator.",
    )


class BGPMissingAdvertise(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "bgp_missing_route_advertisement"
    description = "An expected BGP route advertisement is missing."
    effect_property = "reachability"
    TAGS: str = ["bgp"]
    supported_backends = ("kathara", "containerlab")

    Params = BGPMissingAdvertiseParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._inject_mode: str | None = None
        self._withdrawn_prefix: str | None = None

    def root_cause_resources(self, params: BGPMissingAdvertiseParams):
        return [node_resource(params.host_name)]

    def _is_enterprise_branch(self) -> bool:
        name = (self.scenario_name or "").lower()
        return name == "enterprise_branch" or name.startswith("enterprise_branch")

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

    def _resolve_withdraw_prefix(self, params: BGPMissingAdvertiseParams) -> str:
        if params.prefix:
            return str(ipaddress.ip_network(params.prefix, strict=False))
        return str(
            ipaddress.ip_network(
                resolve_victim_host_ip(self.runtime, params.host_name),
                strict=False,
            )
        )

    def _inject_missing_adv_containerlab(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        prefix = self._resolve_withdraw_prefix(params)
        self._withdrawn_prefix = prefix
        self._inject_mode = "srl_prefix"
        self.runtime.srl_withdraw_bgp_prefix(params.host_name, prefix)
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(SRL export-policy block for {prefix})."
        )

    def _inject_missing_adv_kathara(self, params: BGPMissingAdvertiseParams) -> None:
        if self._is_enterprise_branch():
            self._inject_missing_adv_enterprise_redistribute(params)
            return
        if params.prefix:
            self._inject_missing_adv_prefix(params, params.prefix)
            return
        self._inject_missing_adv_bgp_networks(params)

    def _inject_missing_adv_prefix(
        self, params: BGPMissingAdvertiseParams, prefix: str
    ) -> None:
        prefix = str(ipaddress.ip_network(prefix, strict=False))
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        cmd = (
            "vtysh -c 'configure terminal' "
            f"-c 'router bgp {asn}' "
            f"-c 'no network {prefix}' "
            "-c 'address-family ipv4 unicast' "
            f"-c 'no network {prefix}' "
            "-c 'end' "
            "-c 'write memory'"
        )
        self.runtime.exec(params.host_name, cmd)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true",
        )
        self._withdrawn_prefix = prefix
        self._inject_mode = "prefix"
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(withdrew network {prefix})."
        )

    def _inject_missing_adv_bgp_networks(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        """Withdraw every BGP ``network`` statement via vtysh (leave OSPF alone)."""
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        cfg = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
        )
        prefixes: list[str] = []
        in_bgp = False
        for line in cfg.splitlines():
            s = line.lstrip()
            if s.startswith("router bgp"):
                in_bgp = True
                continue
            if s.startswith("router ") and not s.startswith("router bgp"):
                in_bgp = False
                continue
            if not in_bgp or not s.startswith("network "):
                continue
            rest = s[len("network ") :].strip()
            # OSPF uses ``network … area …``; BGP uses ``network <prefix>``.
            if " area " in rest:
                continue
            token = rest.split()[0] if rest else ""
            if token:
                prefixes.append(token)
        if not prefixes:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: no BGP network statements on "
                f"{params.host_name!r} to withdraw."
            )
        for prefix in prefixes:
            cmd = (
                "vtysh -c 'configure terminal' "
                f"-c 'router bgp {asn}' "
                f"-c 'no network {prefix}' "
                "-c 'address-family ipv4 unicast' "
                f"-c 'no network {prefix}' "
                "-c 'end'"
            )
            self.runtime.exec(params.host_name, cmd)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'write memory' 2>/dev/null || true",
        )
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true",
        )
        self._inject_mode = "bgp_network"
        if len(prefixes) == 1:
            self._withdrawn_prefix = prefixes[0]
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(withdrew BGP networks {prefixes})."
        )

    def _inject_missing_adv_enterprise_redistribute(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        # Overlay VRFs advertise LAN prefixes via redistribute connected.
        for vrf in ("vrf_corp", "vrf_server"):
            cmd = (
                "vtysh -c 'configure terminal' "
                f"-c 'router bgp {asn} vrf {vrf}' "
                "-c 'address-family ipv4 unicast' "
                "-c 'no redistribute connected' "
                "-c 'end'"
            )
            self.runtime.exec(params.host_name, f"{cmd} 2>/dev/null || true")
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'write memory' 2>/dev/null || true",
        )
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true",
        )
        self._inject_mode = "redistribute"
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            "(disabled redistribute connected under BGP VRFs)."
        )

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
            self, "_withdrawn_prefix", None
        ) or self._resolve_withdraw_prefix(params)
        verified = self.runtime.srl_bgp_prefix_withdrawn(params.host_name, prefix)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "prefix": prefix,
                "mode": getattr(self, "_inject_mode", None) or "srl_prefix",
            },
        )

    def _count_running_networks(self, host: str, *, section: str) -> int:
        """Count ``network`` lines under ``router bgp`` or ``router ospf``."""
        if section == "bgp":
            awk = (
                "BEGIN{in_sec=0} "
                "/^router bgp/{in_sec=1; next} "
                "/^router /{in_sec=0} "
                "in_sec && /^[[:space:]]*network /{c++} "
                "END{print c+0}"
            )
        else:
            awk = (
                "BEGIN{in_sec=0} "
                "/^router ospf/{in_sec=1; next} "
                "/^router /{in_sec=0} "
                "in_sec && /^[[:space:]]*network /{c++} "
                "END{print c+0}"
            )
        raw = self.runtime.exec(
            host,
            f"vtysh -c 'show running-config' 2>/dev/null | awk '{awk}' || echo 0",
        ).strip()
        try:
            return int(raw.splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    def _verify_missing_adv_kathara(self, params: BGPMissingAdvertiseParams) -> dict:
        mode = self._inject_mode
        if mode is None:
            if self._is_enterprise_branch():
                mode = "redistribute"
            elif params.prefix:
                mode = "prefix"
            else:
                mode = "bgp_network"

        if mode == "redistribute":
            raw = self.runtime.exec(
                params.host_name,
                "vtysh -c 'show running-config' 2>/dev/null "
                "| grep -c '^[[:space:]]*redistribute connected' || echo 0",
            ).strip()
            try:
                redist_count = int(raw.splitlines()[-1])
            except (ValueError, IndexError):
                redist_count = -1
            verified = redist_count == 0
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "mode": mode,
                    "redistribute_connected_count": redist_count,
                },
            )

        prefix = params.prefix or self._withdrawn_prefix
        if mode == "prefix" and prefix:
            prefix = str(ipaddress.ip_network(prefix, strict=False))
            cfg = self.runtime.exec(
                params.host_name,
                "vtysh -c 'show running-config' 2>/dev/null",
            )
            # Match BGP network line only (not OSPF ``network … area``).
            present = False
            in_bgp = False
            for line in cfg.splitlines():
                s = line.lstrip()
                if s.startswith("router bgp"):
                    in_bgp = True
                elif s.startswith("router ") and not s.startswith("router bgp"):
                    in_bgp = False
                if in_bgp and (
                    s == f"network {prefix}" or s.startswith(f"network {prefix} ")
                ):
                    present = True
                    break
            verified = not present
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "mode": mode,
                    "prefix": prefix,
                    "network_present": present,
                },
            )

        bgp_count = self._count_running_networks(params.host_name, section="bgp")
        ospf_count = self._count_running_networks(params.host_name, section="ospf")
        verified = bgp_count == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "mode": mode,
                "bgp_network_count": bgp_count,
                "ospf_network_count": ospf_count,
            },
        )


# ==================================================================
""" Problem: BGP static blackhole route misconfiguration problem. """
# ==================================================================


# ==================================================================
""" Problem: BGP blackhole community leak (remote-triggered blackholing). """
# ==================================================================


class BGPBlackholeCommunityLeakParams(BaseModel):
    """Parameters for injecting a BGP blackhole community leak fault."""

    host_name: str = Field(
        description="Target router host name for the export-policy fault."
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional data-plane observer for symptom/verify probes.",
    )
    probe_dst_ip: str | None = Field(
        default=None,
        description="Optional ping destination for symptom/verify probes.",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer/stub host near the leaker.",
    )


class BGPBlackholeCommunityLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "bgp_blackhole_community_leak"
    description = "A provider blackhole BGP community is leaked on export."
    TAGS: str = ["bgp", "isp", "ebgp", "rtbh"]
    COMPATIBLE_COLUMNS = frozenset({"isp_abilene_ebgp_rtbh", "isp_dfn-bwin_ebgp_rtbh"})

    Params = BGPBlackholeCommunityLeakParams

    symptom_desc = (
        "Reachability to a legitimately originated business prefix is lost while "
        "BGP sessions remain up."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPBlackholeCommunityLeakParams):
        return [node_resource(params.host_name)]

    def _rtbh_bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        if not isinstance(bgp, dict) or not bgp.get("rtbh"):
            raise RuntimeCapabilityError(
                f"{type(self).__name__} requires a named eBGP RTBH scenario."
            )
        return bgp

    def inject_fault(self, params: BGPBlackholeCommunityLeakParams):
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot inject_fault: unsupported backend "
                f"{self.lab_backend!r} (Kathara + FRR only)."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = str(bgp.get("leaker_device") or params.host_name)
        if params.host_name != leaker:
            self.logger.warning(
                f"Inject host_name={params.host_name!r} differs from profile "
                f"leaker_device={leaker!r}; applying export policy on leaker."
            )
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        neighbor_ip = str(bgp.get("leaker_to_rtbh_neighbor_ip") or "")
        if not target_prefix or not community or not route_map or not neighbor_ip:
            raise RuntimeCapabilityError(
                f"{type(self).__name__} missing RTBH inventory fields."
            )
        cmd = (
            "vtysh -c 'configure terminal' "
            f"-c 'ip prefix-list TARGET-PREFIX seq 5 permit {target_prefix} le 24' "
            f"-c 'route-map {route_map} permit 5' "
            "-c 'match ip address prefix-list TARGET-PREFIX' "
            f"-c 'set community {community} additive' "
            "-c 'end' "
            "-c 'write memory'"
        )
        self.runtime.exec(leaker, cmd)
        self.runtime.exec(
            leaker,
            f"vtysh -c 'clear ip bgp {neighbor_ip} soft out' 2>/dev/null || true",
        )
        time.sleep(20)
        self.logger.info(
            f"Injected BGP blackhole community leak on {leaker} toward "
            f"{neighbor_ip} for {target_prefix} (community {community})."
        )

    def _bgp_sessions_established(self, devices: list[str]) -> bool:
        for device in devices:
            summary = self.runtime.exec(
                device, "vtysh -c 'show bgp summary' 2>/dev/null || true"
            )
            peers_up = False
            for line in summary.splitlines():
                fields = line.split()
                if len(fields) >= 10 and fields[0].count(".") == 3:
                    if fields[9].isdigit() or fields[9] == "Established":
                        peers_up = True
                        break
            if not peers_up:
                return False
        return True

    def _origin_preserved(
        self,
        *,
        leaker: str,
        origin_device: str,
        target_prefix: str,
        origin_asn: int,
    ) -> bool:
        origin_cfg = self.runtime.exec(
            origin_device, "vtysh -c 'show running-config' 2>/dev/null"
        )
        if f"network {target_prefix}" not in origin_cfg:
            return False
        leaker_bgp = self.runtime.exec(
            leaker,
            f"vtysh -c 'show bgp ipv4 unicast {target_prefix}' 2>/dev/null || true",
        )
        if "Network not in table" in leaker_bgp:
            return False
        if str(origin_asn) not in leaker_bgp:
            return False
        leaker_cfg = self.runtime.exec(
            leaker, "vtysh -c 'show running-config' 2>/dev/null"
        )
        if f"network {target_prefix}" in leaker_cfg:
            return False
        if f"ip route {target_prefix}" in leaker_cfg and "Null0" in leaker_cfg:
            return False
        return True

    def _community_present(
        self, provider: str, target_prefix: str, community: str
    ) -> bool:
        out = self.runtime.exec(
            provider,
            f"vtysh -c 'show bgp ipv4 unicast {target_prefix}' 2>/dev/null || true",
        )
        if "Network not in table" in out:
            return False
        return community in out

    def _rtbh_forwarding_active(
        self, provider: str, target_prefix: str, discard_nh: str
    ) -> bool:
        fib = self.runtime.exec(
            provider,
            f"vtysh -c 'show ip route {target_prefix}' 2>/dev/null || true",
        )
        bgp = self.runtime.exec(
            provider,
            f"vtysh -c 'show bgp ipv4 unicast {target_prefix}' 2>/dev/null || true",
        )
        network = target_prefix.split("/")[0]
        if discard_nh and discard_nh in fib:
            return True
        if "Null0" in fib and network in fib:
            return True
        if discard_nh and discard_nh in bgp:
            return True
        return "blackhole" in fib.lower() and network in fib

    def _dataplane_unreachable(self, observer: str, ping_addr: str) -> bool:
        return not self.runtime.ping_ok(observer, ping_addr, count=3)

    def verify_fault(self, params: BGPBlackholeCommunityLeakParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = str(bgp.get("leaker_device") or params.host_name)
        origin_device = str(bgp.get("legitimate_origin_device") or "")
        provider = str(bgp.get("rtbh_provider_device") or "")
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        ping_addr = params.probe_dst_ip or str(bgp.get("target_ping_address") or "")
        observer = params.symptom_host or str(bgp.get("data_plane_observer_host") or "")
        discard_nh = str(bgp.get("discard_next_hop") or "")
        origin_asn = int(bgp.get("legitimate_origin_asn") or 0)

        origin_ok = self._origin_preserved(
            leaker=leaker,
            origin_device=origin_device,
            target_prefix=target_prefix,
            origin_asn=origin_asn,
        )
        community_ok = self._community_present(provider, target_prefix, community)
        rtbh_ok = self._rtbh_forwarding_active(provider, target_prefix, discard_nh)
        dataplane_ok = self._dataplane_unreachable(observer, ping_addr)
        sessions_ok = self._bgp_sessions_established(
            [d for d in (leaker, origin_device, provider) if d]
        )

        verified = (
            origin_ok and community_ok and rtbh_ok and dataplane_ok and sessions_ok
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "leaker_device": leaker,
                "legitimate_origin_device": origin_device,
                "rtbh_provider_device": provider,
                "target_prefix": target_prefix,
                "blackhole_community": community,
                "observer": observer,
                "dst_ip": ping_addr,
                "origin_preserved": origin_ok,
                "community_present": community_ok,
                "rtbh_forwarding_active": rtbh_ok,
                "dataplane_unreachable": dataplane_ok,
                "sessions_ok": sessions_ok,
            },
        )

    def recover_fault(self, params: BGPBlackholeCommunityLeakParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot recover_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = str(bgp.get("leaker_device") or params.host_name)
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        neighbor_ip = str(bgp.get("leaker_to_rtbh_neighbor_ip") or "")
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        provider = str(bgp.get("rtbh_provider_device") or "")
        ping_addr = params.probe_dst_ip or str(bgp.get("target_ping_address") or "")
        observer = params.symptom_host or str(bgp.get("data_plane_observer_host") or "")
        discard_nh = str(bgp.get("discard_next_hop") or "")

        cmd = (
            "vtysh -c 'configure terminal' "
            f"-c 'no route-map {route_map} permit 5' "
            "-c 'no ip prefix-list TARGET-PREFIX seq 5' "
            "-c 'end' "
            "-c 'write memory'"
        )
        self.runtime.exec(leaker, cmd)
        self.runtime.exec(
            leaker,
            f"vtysh -c 'clear ip bgp {neighbor_ip} soft out' 2>/dev/null || true",
        )
        time.sleep(20)

        community_gone = not self._community_present(provider, target_prefix, community)
        rtbh_cleared = not self._rtbh_forwarding_active(
            provider, target_prefix, discard_nh
        )
        dataplane_ok = self.runtime.ping_ok(observer, ping_addr, count=3)
        sessions_ok = self._bgp_sessions_established([leaker, provider])

        ok = community_gone and rtbh_cleared and dataplane_ok and sessions_ok
        details = {
            "host": params.host_name,
            "leaker_device": leaker,
            "observer": observer,
            "dst_ip": ping_addr,
            "community_gone": community_gone,
            "rtbh_cleared": rtbh_cleared,
            "dataplane_ok": dataplane_ok,
            "sessions_ok": sessions_ok,
        }
        self.logger.info(
            f"recover_fault bgp_blackhole_community_leak: ok={ok} {details}"
        )
        return {"verified": ok, "details": details}


# ==================================================================
""" Problem: BGP export-policy misconfig causing RPKI-invalid route leak. """
# ==================================================================


class BGPRPKIInvalidRouteLeakParams(BaseModel):
    """Parameters for injecting an RPKI-invalid BGP route leak."""

    host_name: str = Field(
        description="Primary leaker router from the ISP RPKI profile inventory."
    )


class BGPRPKIInvalidRouteLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "bgp_rpki_invalid_route_leak"
    description = "RPKI-invalid prefixes are leaked into BGP."
    TAGS: str = ["rpki"]

    Params = BGPRPKIInvalidRouteLeakParams

    symptom_desc = (
        "Some BGP speakers learn RPKI-invalid routes from a leaked origin while "
        "ROV-enabled peers reject them."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPRPKIInvalidRouteLeakParams):
        return [node_resource(params.host_name)]

    def _rpki_bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        if not isinstance(bgp, dict) or not bgp.get("rpki"):
            raise RuntimeCapabilityError(
                f"{type(self).__name__} requires an RPKI ISP scenario "
                "(deploy with: nika env run isp_abilene_ebgp_rpki)."
            )
        return bgp

    def inject_fault(self, params: BGPRPKIInvalidRouteLeakParams):
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot inject_fault: unsupported backend "
                f"{self.lab_backend!r} (Kathara + FRR only)."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = str(bgp.get("leaker_device") or params.host_name)
        if params.host_name != leaker:
            self.logger.warning(
                f"Inject host_name={params.host_name!r} differs from profile "
                f"leaker_device={leaker!r}; applying export permit on leaker AS."
            )
        devices = [str(d) for d in (bgp.get("leaker_as_devices") or [leaker])]
        # Permit LEAK prefixes on eBGP export (replaces healthy deny seq 5).
        for device in devices:
            cmd = (
                "vtysh -c 'configure terminal' "
                "-c 'route-map BGP-OUT permit 5' "
                "-c 'match ip address prefix-list LEAK' "
                "-c 'end' "
                "-c 'write memory'"
            )
            self.runtime.exec(device, cmd)
            self.runtime.exec(
                device, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
        # Allow eBGP export + ROV evaluation to settle before verify_fault.
        time.sleep(25)
        self.logger.info(
            f"Injected RPKI-invalid route leak via incorrect BGP export policy "
            f"on leaker AS devices {devices} (primary={leaker})."
        )

    def verify_fault(self, params: BGPRPKIInvalidRouteLeakParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = str(bgp.get("leaker_device") or params.host_name)
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        rov = str(bgp.get("rov_observer") or "")
        non_rov = str(bgp.get("non_rov_observer") or "")
        leaker_asn = int(bgp.get("leaker_asn") or 0)

        advertised = True
        for prefix in prefixes:
            out = self.runtime.exec(
                leaker, f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true"
            )
            if prefix.split("/")[0] not in out and prefix not in out:
                advertised = False
                break

        non_rov_learned = True
        for prefix in prefixes:
            out = self.runtime.exec(
                non_rov,
                f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true",
            )
            if "Network not in table" in out:
                non_rov_learned = False
                break
            if prefix.split("/")[0] not in out and prefix not in out:
                non_rov_learned = False
                break
            if leaker_asn and str(leaker_asn) not in out:
                non_rov_learned = False
                break

        rov_rejected = True
        for prefix in prefixes:
            out = self.runtime.exec(
                rov, f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true"
            )
            if "Network not in table" in out:
                continue
            network = prefix.split("/")[0]
            if network in out or prefix in out:
                if "Invalid" in out and (
                    "not located" in out.lower() or "*" not in out
                ):
                    continue
                if "*" in out or "bestpath" in out.lower():
                    rov_rejected = False
                    break

        sessions_ok = True
        for device in (leaker, rov, non_rov):
            summary = self.runtime.exec(
                device, "vtysh -c 'show bgp summary' 2>/dev/null || true"
            )
            peers_up = False
            for line in summary.splitlines():
                fields = line.split()
                if len(fields) >= 10 and fields[0].count(".") == 3:
                    if fields[9].isdigit() or fields[9] == "Established":
                        peers_up = True
                        break
            if not peers_up:
                sessions_ok = False
                break

        verified = advertised and non_rov_learned and rov_rejected and sessions_ok
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "leaker_device": leaker,
                "leak_prefixes": prefixes,
                "advertised": advertised,
                "non_rov_learned": non_rov_learned,
                "rov_rejected": rov_rejected,
                "sessions_ok": sessions_ok,
                "rov_observer": rov,
                "non_rov_observer": non_rov,
            },
        )

    def recover_fault(self, params: BGPRPKIInvalidRouteLeakParams) -> dict:
        """Restore healthy BGP-OUT deny for leak prefixes on the leaker AS."""
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot recover_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = str(bgp.get("leaker_device") or params.host_name)
        devices = [str(d) for d in (bgp.get("leaker_as_devices") or [leaker])]
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        observers = [
            str(bgp.get("rov_observer") or ""),
            str(bgp.get("non_rov_observer") or ""),
        ]
        observers = [o for o in observers if o]

        for device in devices:
            cmd = (
                "vtysh -c 'configure terminal' "
                "-c 'route-map BGP-OUT deny 5' "
                "-c 'match ip address prefix-list LEAK' "
                "-c 'end' "
                "-c 'write memory'"
            )
            self.runtime.exec(device, cmd)
            self.runtime.exec(
                device, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
        time.sleep(25)

        leak_absent = True
        for observer in observers:
            for prefix in prefixes:
                out = self.runtime.exec(
                    observer,
                    f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true",
                )
                if "Network not in table" in out:
                    continue
                network = prefix.split("/")[0]
                if network in out or prefix in out:
                    if "from" in out.lower() or "Path" in out or "*" in out:
                        leak_absent = False
                        break
            if not leak_absent:
                break

        ok = leak_absent
        details = {
            "host": params.host_name,
            "leaker_device": leaker,
            "leaker_as_devices": devices,
            "leak_prefixes": prefixes,
            "leak_absent": leak_absent,
            "observers": observers,
        }
        self.logger.info(
            f"recover_fault bgp_rpki_invalid_route_leak: ok={ok} {details}"
        )
        return {"verified": ok, "details": details}


# ==================================================================
""" Problem: BGP maximum-prefix exceeded (Optus 2023-inspired). """
# ==================================================================

_FLOOD_PREFIX_BASE = "198.19"
_MAX_PREFIX_EVIDENCE = (
    "maximum-prefix",
    "maximum prefix",
    "maxpfx",
    "prefix count exceeded",
    "prefix limit",
)


class BGPMaxPrefixExceededParams(BaseModel):
    """Parameters for injecting a BGP maximum-prefix session reset."""

    receiver_name: str = Field(
        description="Router that enforces maximum-prefix on the eBGP session."
    )
    peer_name: str = Field(
        description="eBGP peer that advertises an excessive prefix set."
    )
    neighbor_ip: str | None = Field(
        default=None,
        description="Peer address as seen from the receiver; resolved from inventory when omitted.",
    )
    maximum_prefix: int | None = Field(
        default=None,
        description="Receiver maximum-prefix threshold; derived from current received count when omitted.",
    )
    flood_count: int = Field(
        default=40,
        ge=5,
        description="Number of /24 test prefixes the peer advertises during inject.",
    )


class BGPMaxPrefixExceeded(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "bgp_max_prefix_exceeded"
    description = "BGP maximum-prefix limit is exceeded on a session."
    TAGS: str = ["bgp", "isp"]
    COMPATIBLE_COLUMNS = frozenset({"isp_abilene/ebgp", "isp_geant/ebgp"})

    Params = BGPMaxPrefixExceededParams

    symptom_desc = (
        "An eBGP peer advertises more prefixes than the receiver's configured "
        "maximum-prefix, resetting the session and withdrawing learned routes."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._max_prefix_state: dict | None = None

    def root_cause_resources(self, params: BGPMaxPrefixExceededParams):
        return [
            node_resource(params.receiver_name),
            node_resource(params.peer_name),
        ]

    def _bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        return bgp if isinstance(bgp, dict) else {}

    def _resolve_session(
        self, params: BGPMaxPrefixExceededParams
    ) -> tuple[str, str, str]:
        receiver = params.receiver_name
        peer = params.peer_name
        neighbor_ip = params.neighbor_ip
        if neighbor_ip:
            return receiver, peer, neighbor_ip

        bgp = self._bgp_inventory()
        sessions = [
            s
            for s in (bgp.get("sessions") or [])
            if str(s.get("session_type") or "") == "ebgp"
            and str(s.get("local_device") or "") == receiver
            and str(s.get("remote_device") or "") == peer
            and s.get("remote_ip")
        ]
        if sessions:
            sessions = sorted(sessions, key=lambda s: str(s.get("remote_ip") or ""))
            return receiver, peer, str(sessions[0]["remote_ip"])

        # Fall back to any eBGP session matching the device pair (either direction).
        for s in bgp.get("sessions") or []:
            if str(s.get("session_type") or "") != "ebgp":
                continue
            local = str(s.get("local_device") or "")
            remote = str(s.get("remote_device") or "")
            if {local, remote} == {receiver, peer} and s.get("remote_ip"):
                if local == receiver:
                    return receiver, peer, str(s["remote_ip"])
                if s.get("local_ip"):
                    return receiver, peer, str(s["local_ip"])

        raise RuntimeCapabilityError(
            f"{type(self).__name__} could not resolve eBGP neighbor IP for "
            f"receiver={receiver!r} peer={peer!r}; pass neighbor_ip or deploy "
            "isp with --bgp-mode ebgp."
        )

    def _neighbor_output(self, router: str, neighbor_ip: str) -> str:
        return self.runtime.exec(
            router,
            f"vtysh -c 'show bgp neighbors {neighbor_ip}' 2>/dev/null || true",
        )

    def _session_established(self, router: str, neighbor_ip: str) -> bool:
        out = self._neighbor_output(router, neighbor_ip)
        for line in out.splitlines():
            lower = line.lower()
            if "bgp state" in lower and "established" in lower:
                return True
        return False

    def _received_prefix_count(self, router: str, neighbor_ip: str) -> int:
        out = self._neighbor_output(router, neighbor_ip)
        for pattern in (
            r"Prefix received(?: count)?:\s*(\d+)",
            r"Accepted prefixes?:\s*(\d+)",
            r"prefixes received[:\s]+(\d+)",
        ):
            match = re.search(pattern, out, re.IGNORECASE)
            if match:
                return int(match.group(1))
        summary = self.runtime.exec(
            router, "vtysh -c 'show bgp summary' 2>/dev/null || true"
        )
        for line in summary.splitlines():
            fields = line.split()
            if not fields or fields[0] != neighbor_ip:
                continue
            if len(fields) >= 10 and fields[-1].isdigit():
                return int(fields[-1])
        return 0

    def _max_prefix_evidence(self, router: str, neighbor_ip: str) -> bool:
        neighbor = self._neighbor_output(router, neighbor_ip)
        blob = neighbor.lower()
        if any(token in blob for token in _MAX_PREFIX_EVIDENCE):
            return True
        if "last reset" in blob and "prefix" in blob:
            return True
        logs = self.runtime.exec(
            router,
            "grep -Ei 'maximum.?prefix|maxpfx|prefix.?limit|prefix count exceeded' "
            "/var/log/frr/*.log /var/log/syslog 2>/dev/null | tail -n 40 || true",
        )
        return bool(logs.strip()) and any(
            token in logs.lower() for token in _MAX_PREFIX_EVIDENCE
        )

    def _business_prefix_missing(self, receiver: str, peer: str) -> bool:
        bgp = self._bgp_inventory()
        peer_prefixes = [
            str(item.get("prefix") or "")
            for item in (bgp.get("originated") or [])
            if str(item.get("device") or "") == peer and item.get("prefix")
        ]
        if not peer_prefixes:
            # Fall back to any originated prefix not owned by the receiver.
            peer_prefixes = [
                str(item.get("prefix") or "")
                for item in (bgp.get("originated") or [])
                if str(item.get("device") or "") != receiver and item.get("prefix")
            ]
        if not peer_prefixes:
            return True
        missing = 0
        for prefix in peer_prefixes:
            out = self.runtime.exec(
                receiver,
                f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true",
            )
            if "Network not in table" in out:
                missing += 1
                continue
            network = prefix.split("/")[0]
            if network not in out and prefix not in out:
                missing += 1
        return missing == len(peer_prefixes)

    def _install_flood_policy(self, device: str) -> None:
        cmd = (
            "vtysh -c 'configure terminal' "
            "-c 'ip prefix-list FLOOD seq 5 permit 198.19.0.0/16 le 24' "
            "-c 'route-map BGP-OUT permit 1' "
            "-c 'match ip address prefix-list FLOOD' "
            "-c 'route-map BGP-IN permit 1' "
            "-c 'match ip address prefix-list FLOOD' "
            "-c 'end'"
        )
        self.runtime.exec(device, cmd)

    def _remove_flood_policy(self, device: str) -> None:
        cmd = (
            "vtysh -c 'configure terminal' "
            "-c 'no route-map BGP-OUT permit 1' "
            "-c 'no route-map BGP-IN permit 1' "
            "-c 'no ip prefix-list FLOOD' "
            "-c 'end'"
        )
        self.runtime.exec(device, cmd)

    def _flood_prefixes(self, count: int) -> list[str]:
        return [f"{_FLOOD_PREFIX_BASE}.{i}.0/24" for i in range(count)]

    def _advertise_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        parts = [
            "vtysh -c 'configure terminal'",
            f"-c 'router bgp {asn}'",
            "-c 'address-family ipv4 unicast'",
        ]
        for prefix in prefixes:
            parts.append(f"-c 'network {prefix}'")
        parts.extend(["-c 'end'", "-c 'write memory'"])
        self.runtime.exec(peer, " ".join(parts))
        self.runtime.exec(
            peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
        )

    def _withdraw_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        parts = [
            "vtysh -c 'configure terminal'",
            f"-c 'router bgp {asn}'",
            "-c 'address-family ipv4 unicast'",
        ]
        for prefix in prefixes:
            parts.append(f"-c 'no network {prefix}'")
        parts.extend(["-c 'end'", "-c 'write memory'"])
        self.runtime.exec(peer, " ".join(parts))

    def _set_maximum_prefix(
        self, receiver: str, asn: int, neighbor_ip: str, limit: int
    ) -> None:
        cmd = (
            "vtysh -c 'configure terminal' "
            f"-c 'router bgp {asn}' "
            "-c 'address-family ipv4 unicast' "
            f"-c 'neighbor {neighbor_ip} maximum-prefix {limit}' "
            "-c 'end' "
            "-c 'write memory'"
        )
        self.runtime.exec(receiver, cmd)

    def _clear_maximum_prefix(self, receiver: str, asn: int, neighbor_ip: str) -> None:
        cmd = (
            "vtysh -c 'configure terminal' "
            f"-c 'router bgp {asn}' "
            "-c 'address-family ipv4 unicast' "
            f"-c 'no neighbor {neighbor_ip} maximum-prefix' "
            "-c 'end' "
            "-c 'write memory'"
        )
        self.runtime.exec(receiver, cmd)

    def _wait_session_state(
        self,
        router: str,
        neighbor_ip: str,
        *,
        established: bool,
        timeout_s: float = 90.0,
        poll_s: float = 2.0,
    ) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._session_established(router, neighbor_ip) == established:
                return True
            time.sleep(poll_s)
        return self._session_established(router, neighbor_ip) == established

    def inject_fault(self, params: BGPMaxPrefixExceededParams):
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot inject_fault: unsupported backend "
                f"{self.lab_backend!r} (Kathara + FRR only)."
            )
        receiver, peer, neighbor_ip = self._resolve_session(params)
        if not self._session_established(receiver, neighbor_ip):
            # Allow BGP to finish converging after deploy.
            if not self._wait_session_state(
                receiver, neighbor_ip, established=True, timeout_s=180.0
            ):
                raise RuntimeError(
                    f"eBGP session {receiver}←{neighbor_ip} ({peer}) not Established "
                    "before maximum-prefix inject."
                )

        received = self._received_prefix_count(receiver, neighbor_ip)
        limit = params.maximum_prefix
        if limit is None:
            limit = max(received + 5, 10)
        flood_count = max(params.flood_count, limit + 15)
        prefixes = self._flood_prefixes(flood_count)

        receiver_asn = self.runtime.frr_get_bgp_asn_number(receiver)
        peer_asn = self.runtime.frr_get_bgp_asn_number(peer)

        self._set_maximum_prefix(receiver, receiver_asn, neighbor_ip, limit)
        # Confirm session still up with received < limit before the flood.
        if not self._session_established(receiver, neighbor_ip):
            raise RuntimeError(
                "maximum-prefix configuration unexpectedly reset the eBGP session "
                "before the peer flood."
            )
        if self._received_prefix_count(receiver, neighbor_ip) >= limit:
            raise RuntimeError(
                f"received prefixes already >= maximum-prefix {limit} before flood."
            )

        self._install_flood_policy(peer)
        self._install_flood_policy(receiver)
        self._advertise_flood(peer, peer_asn, prefixes)

        exceeded_seen = False
        deadline = time.time() + 90.0
        while time.time() < deadline:
            count = self._received_prefix_count(receiver, neighbor_ip)
            if count > limit:
                exceeded_seen = True
            if not self._session_established(receiver, neighbor_ip):
                break
            time.sleep(2.0)

        if self._session_established(receiver, neighbor_ip):
            # Force a refresh if FRR has not yet applied the limit.
            self.runtime.exec(
                peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
            self._wait_session_state(
                receiver, neighbor_ip, established=False, timeout_s=60.0
            )

        self._max_prefix_state = {
            "receiver": receiver,
            "peer": peer,
            "neighbor_ip": neighbor_ip,
            "receiver_asn": receiver_asn,
            "peer_asn": peer_asn,
            "limit": limit,
            "prefixes": prefixes,
            "exceeded_seen": exceeded_seen,
        }
        self.logger.info(
            f"Injected bgp_max_prefix_exceeded: peer={peer} flooded "
            f"{len(prefixes)} prefixes toward receiver={receiver} "
            f"neighbor={neighbor_ip} maximum-prefix={limit}."
        )

    def verify_fault(self, params: BGPMaxPrefixExceededParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        receiver, peer, neighbor_ip = self._resolve_session(params)
        session_down = not self._session_established(receiver, neighbor_ip)
        evidence = self._max_prefix_evidence(receiver, neighbor_ip)
        routes_missing = self._business_prefix_missing(receiver, peer)
        exceeded_seen = bool((self._max_prefix_state or {}).get("exceeded_seen"))
        # FRR often tears the session down during UPDATE processing, so a
        # stable Established sample with received > limit may never appear.
        verified = session_down and evidence
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "receiver": receiver,
                "peer": peer,
                "neighbor_ip": neighbor_ip,
                "session_down": session_down,
                "max_prefix_evidence": evidence,
                "business_routes_missing": routes_missing,
                "exceeded_seen_during_inject": exceeded_seen,
                "limit": (self._max_prefix_state or {}).get("limit"),
            },
        )

    def _discover_flood_prefixes(self, peer: str) -> list[str]:
        conf = self.runtime.exec(
            peer, "vtysh -c 'show running-config' 2>/dev/null || true"
        )
        found = re.findall(
            rf"network\s+({re.escape(_FLOOD_PREFIX_BASE)}\.\d+\.0/24)", conf
        )
        if found:
            return sorted(set(found))
        return []

    def recover_fault(self, params: BGPMaxPrefixExceededParams) -> dict:
        """Withdraw flood prefixes, remove max-prefix, restore eBGP convergence."""
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot recover_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        state = self._max_prefix_state or {}
        receiver, peer, neighbor_ip = self._resolve_session(params)
        receiver = str(state.get("receiver") or receiver)
        peer = str(state.get("peer") or peer)
        neighbor_ip = str(state.get("neighbor_ip") or neighbor_ip)
        prefixes = list(state.get("prefixes") or [])
        if not prefixes:
            prefixes = self._discover_flood_prefixes(peer)
        if not prefixes:
            prefixes = self._flood_prefixes(max(params.flood_count, 80))
        receiver_asn = int(
            state.get("receiver_asn") or self.runtime.frr_get_bgp_asn_number(receiver)
        )
        peer_asn = int(
            state.get("peer_asn") or self.runtime.frr_get_bgp_asn_number(peer)
        )

        self._withdraw_flood(peer, peer_asn, prefixes)
        self._remove_flood_policy(peer)
        self._remove_flood_policy(receiver)
        self._clear_maximum_prefix(receiver, receiver_asn, neighbor_ip)
        self.runtime.exec(
            receiver,
            f"vtysh -c 'clear ip bgp {neighbor_ip}' 2>/dev/null || true",
        )
        self.runtime.exec(
            peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
        )

        session_up = self._wait_session_state(
            receiver, neighbor_ip, established=True, timeout_s=120.0
        )
        routes_ok = not self._business_prefix_missing(receiver, peer)
        # Business routes may need a short extra settle after session up.
        if session_up and not routes_ok:
            time.sleep(15)
            routes_ok = not self._business_prefix_missing(receiver, peer)
        ok = session_up and routes_ok
        details = {
            "receiver": receiver,
            "peer": peer,
            "neighbor_ip": neighbor_ip,
            "session_established": session_up,
            "business_routes_restored": routes_ok,
            "withdrawn_prefixes": len(prefixes),
        }
        self.logger.info(f"recover_fault bgp_max_prefix_exceeded: ok={ok} {details}")
        self._max_prefix_state = None
        return {"verified": ok, "details": details}
