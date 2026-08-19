import ipaddress
import re
import time

from pydantic import BaseModel, Field

from nika.problems.inject_resolve import (
    resolve_victim_host,
    resolve_victim_host_ip,
)
from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
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
    effect_protocol = "bgp"
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
            fault_type=self.root_cause_name,
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
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "bgp_missing_route_advertisement"
    effect_property = "reachability"
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
            fault_type=self.root_cause_name,
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
            fault_type=self.root_cause_name,
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


# ==================================================================
""" Problem: BGP blackhole route advertisement misconfiguration problem. """
# ==================================================================


class BGPBlackholeRouteLeakParams(BaseModel):
    """Parameters for injecting a BGP blackhole route leak fault."""

    host_name: str = Field(description="Target router host name.")


class BGPBlackholeRouteLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
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
                    fault_type=self.root_cause_name,
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
                    fault_type=self.root_cause_name,
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
                f"{type(self).__name__} requires isp with RPKI enabled "
                "(deploy with: nika env run isp --bgp-mode ebgp --rpki)."
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
        return {"ok": ok, "details": details}


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
    TAGS: str = ["bgp", "isp"]

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
                receiver, neighbor_ip, established=True, timeout_s=120.0
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
        return {"ok": ok, "details": details}
