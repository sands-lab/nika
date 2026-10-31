import ipaddress
import json
import random
import re
import time

from pydantic import BaseModel, Field

from nika.net_env.isp.bgp.frr import EXPORT_DENY_PREFIX_LIST
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.inject_resolve import resolve_victim_host_ip
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import RouterFaultOps, router_fault_ops
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import (
    choice,
    first,
    isp_protocol_for,
    loaded_inventory,
    replace,
    require_role_member,
    role_subset,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger
from nika.utils.parallel import bounded_parallel_map


def _compile_isp_bgp_inventory(
    scenario: str, *, igp: str, bgp_mode: str, rpki: bool = False, rtbh: bool = False
):
    """Compile the ISP plan for ``scenario`` and return it with its BGP inventory."""
    from nika.net_env.isp.bgp import compile_bgp_plan
    from nika.net_env.isp.identity import isp_topo_from_scenario
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan

    isp_plan = compile_isp_plan(
        IspConfig(topology=isp_topo_from_scenario(scenario), igp=igp)  # type: ignore[arg-type]
    )
    bgp = compile_bgp_plan(isp_plan, bgp_mode, rpki=rpki, rtbh=rtbh)
    assert bgp is not None
    return isp_plan, bgp.inventory


def _ensure_bgp_inventory(problem_cls, ctx) -> tuple[dict, dict]:
    """Return ``(inventory, bgp_inventory)`` with BGP originators for an ISP scenario.

    Uses the loaded scenario inventory when it has originated prefixes, else
    compiles the ISP plan from ``ctx.isp_options`` (or the failure's protocol needs).
    """
    inventory = loaded_inventory(ctx.net_env)
    bgp_inv = inventory.get("bgp")
    if not isinstance(bgp_inv, dict) or not bgp_inv.get("originated"):
        isp_opts = ctx.isp_options or isp_protocol_for(problem_cls, {"bgp"})
        rtbh = ctx.scenario.endswith("_ebgp_rtbh") or bool(isp_opts.get("rtbh"))
        isp_plan, bgp_inv = _compile_isp_bgp_inventory(
            ctx.scenario,
            igp=str(isp_opts.get("igp") or "ospf"),
            bgp_mode=str(isp_opts.get("bgp_mode") or "ebgp"),
            rpki=bool(isp_opts.get("rpki")),
            rtbh=rtbh,
        )
        inventory = dict(isp_plan.inventory)
        inventory["bgp"] = bgp_inv
    return inventory, bgp_inv


# ==================================================================
""" Problem: BGP ASN misconfiguration. """
# ==================================================================


# bgp_asn_misconfig moves the local ASN by this fixed offset.
ASN_MISCONFIG_OFFSET = 600


class BGPAsnMisconfigParams(BaseModel):
    """Parameters for injecting a BGP ASN misconfiguration fault."""

    host_name: str = Field(description="Target router host name.")


class BGPAsnMisconfig(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_asn_misconfig"
    description = "BGP local ASN is misconfigured relative to peer expectation."
    effect_protocol = "bgp"
    TAGS: str = ["bgp"]
    INCOMPATIBLE_SCENARIOS = {
        "routeros_simple_bgp": "The injector reads and edits the FRR BGP "
        "configuration, which RouterOS routers do not have."
    }
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset({"bgp_asn", "set_bgp_asn"})

    Params = BGPAsnMisconfigParams
    BENCHMARK_COORDINATES = frozenset({"bgp_acl_block"})

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        # Transit routers without BGP (enterprise ISP cores) have no session.
        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "bgp"
        )

    @classmethod
    def coordinate_benchmark_inject(cls, ctx, params_by_problem):
        # Misconfigure a router other than the ACL-blocked one.
        acl = dict(params_by_problem["bgp_acl_block"])
        asn = dict(params_by_problem["bgp_asn_misconfig"])
        routers = list(ctx.net_env.routers or [])
        if acl.get("host_name") == asn.get("host_name") and len(routers) >= 2:
            asn["host_name"] = (
                routers[1] if routers[0] == acl.get("host_name") else routers[0]
            )
        params_by_problem["bgp_asn_misconfig"] = asn
        return params_by_problem

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
        wrong_asn = as_number + ASN_MISCONFIG_OFFSET
        self.runtime.srl_set_bgp_as(params.host_name, wrong_asn)
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} "
            f"from ASN {as_number} to {wrong_asn} (SRL)."
        )

    def _inject_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> None:
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            self._inject_asn_misconfig_native(params, ops)
            return
        as_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
        wrong_asn = as_number + ASN_MISCONFIG_OFFSET
        # k8s_lab uses split FRR configs (/etc/frr/bgpd.conf); campus etc. use frr.conf.
        # Patch the on-disk config so it matches what the running bgpd will use.
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
        # FRR cannot renumber a live BGP instance in place, so delete it and
        # reload the patched config through vtysh. The daemons keep running:
        # killing them leaves watchfrr holding its pid lock, so a restart fails
        # and bgpd stays down instead of running with the wrong ASN (#265).
        # VRF instances depend on the default one and must be removed first.
        instances = [
            line.strip()
            for line in self.runtime.exec(
                params.host_name,
                "vtysh -c 'show running-config' 2>/dev/null | grep -E '^router bgp '",
                timeout=20,
            ).splitlines()
            if line.startswith("router bgp ")
        ]
        instances.sort(key=lambda line: " vrf " not in line)
        if instances:
            remove = " ".join(f"-c 'no {line}'" for line in instances)
            self.runtime.exec(
                params.host_name,
                f"vtysh -c 'configure terminal' {remove}",
                timeout=30,
            )
        for conf in patched:
            self.runtime.exec(params.host_name, f"vtysh -f {conf}", timeout=60)
        wait_until(lambda: self._running_asn(params.host_name) == wrong_asn, 30)
        running = self._running_asn(params.host_name)
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

    def _inject_asn_misconfig_native(
        self, params: BGPAsnMisconfigParams, ops: RouterFaultOps
    ) -> None:
        as_number = ops.bgp_asn()
        wrong_asn = as_number + ASN_MISCONFIG_OFFSET
        ops.set_bgp_asn(wrong_asn)
        wait_until(lambda: ops.bgp_asn() == wrong_asn, 8)
        running = ops.bgp_asn()
        if running != wrong_asn:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: {ops.profile} on {params.host_name!r} "
                f"did not apply ASN change ({as_number} -> {wrong_asn}); "
                f"running={running}."
            )
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} "
            f"from ASN {as_number} to {wrong_asn} ({ops.profile})."
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

    def _running_asn(self, host: str) -> int | None:
        """Local AS of the running bgpd, or ``None`` while it does not answer."""
        summary = self.runtime.exec(
            host, "vtysh -c 'show bgp summary json' 2>/dev/null"
        )
        match = re.search(r'"as"\s*:\s*(\d+)', summary)
        return int(match.group(1)) if match else None

    def _verify_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> dict:
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            return self._verify_asn_misconfig_native(params, ops)
        orig_asn = getattr(self, "_orig_asn", None)
        expected_wrong = getattr(self, "_wrong_asn", None)
        # Only the running bgpd counts: the on-disk file is what inject wrote.
        running_asn = self._running_asn(params.host_name)
        verified = expected_wrong is not None and running_asn == expected_wrong
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "orig_asn": orig_asn,
                "running_asn": running_asn,
                "wrong_asn": expected_wrong,
            },
        )

    def _verify_asn_misconfig_native(
        self, params: BGPAsnMisconfigParams, ops: RouterFaultOps
    ) -> dict:
        orig_asn = getattr(self, "_orig_asn", None)
        wrong_asn = getattr(self, "_wrong_asn", None)
        try:
            running_asn = ops.bgp_asn()
        except RuntimeCapabilityError:
            raise
        except Exception:
            running_asn = None
        verified = (wrong_asn is not None and running_asn == wrong_asn) or (
            orig_asn is not None and running_asn is not None and running_asn != orig_asn
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
    # Remote speakers lose the route. Reachability can survive when the IGP
    # also carries the prefix (see INCOMPATIBLE_SCENARIOS).
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_missing_route_advertisement"
    description = "An expected BGP route advertisement is missing."
    effect_property = "reachability"
    TAGS: str = ["bgp"]
    INCOMPATIBLE_SCENARIOS = {
        "min3clos": "The IGP also carries the host subnets, so peers keep a route "
        "after BGP stops advertising it.",
        "routeros_simple_bgp": "The injector reads and edits the FRR BGP "
        "configuration, which RouterOS routers do not have.",
    }
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset({"bgp_networks", "withdraw_bgp_network"})

    Params = BGPMissingAdvertiseParams

    BENCHMARK_TARGET_ROLE = "bgp_originators"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            from nika.net_env.isp.inject_targets import isp_inject_params

            inventory, bgp_inv = _ensure_bgp_inventory(cls, ctx)
            return dict(isp_inject_params(ctx.problem, inventory, bgp_inv))
        advertise_pool = role_subset(ctx.router_pool, ctx.roles, "bgp_originators")
        return {
            "host_name": choice(
                ctx.rng, advertise_pool, first(advertise_pool) or ctx.router0
            )
        }

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        from nika.net_env.isp.identity import is_isp_scenario

        if not is_isp_scenario(ctx.scenario):
            return super().benchmark_inject_options(ctx, base)
        from nika.net_env.isp.inject_targets import enrich_isp_symptom_params

        inventory, bgp = _ensure_bgp_inventory(cls, ctx)
        rows = []
        for item in sorted(
            bgp.get("originated") or [],
            key=lambda row: (
                str(row.get("device") or ""),
                str(row.get("prefix") or ""),
            ),
        ):
            host = str(item.get("device") or "")
            prefix = str(item.get("prefix") or "")
            if not host:
                continue
            row = replace(base, host_name=host)
            if prefix:
                row["prefix"] = prefix
            for key in ("symptom_host", "probe_dst_ip", "peer_host"):
                row.pop(key, None)
            enrich_isp_symptom_params(row, ctx.problem, inventory, bgp)
            rows.append(row)
        return rows

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.net_env.isp.identity import is_isp_scenario

        host_name = inject.get("host_name")
        if not host_name:
            return
        if not is_isp_scenario(ctx.scenario):
            require_role_member(
                ctx,
                cls.root_cause_name,
                host_name,
                "bgp_originators",
                reason="has no BGP network statement",
            )
            return
        bgp_inv = loaded_inventory(ctx.net_env).get("bgp")
        if isinstance(bgp_inv, dict) and bgp_inv.get("originated"):
            originators = sorted(
                {
                    str(item["device"])
                    for item in bgp_inv["originated"]
                    if item.get("device")
                }
            )
            if originators and host_name not in originators:
                raise ValueError(
                    f"{cls.root_cause_name} host_name={host_name!r} is "
                    f"not a BGP originator on {ctx.scenario}; use one of: {originators}"
                )

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
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            self._inject_missing_adv_native(params, ops)
            return
        if self._is_enterprise_branch():
            self._inject_missing_adv_enterprise_redistribute(params)
            return
        if params.prefix:
            self._inject_missing_adv_prefix(params, params.prefix)
            return
        self._inject_missing_adv_bgp_networks(params)

    def _inject_missing_adv_native(
        self, params: BGPMissingAdvertiseParams, ops: RouterFaultOps
    ) -> None:
        if params.prefix:
            prefix = str(ipaddress.ip_network(params.prefix, strict=False))
            ops.withdraw_bgp_network(prefix)
            self._withdrawn_prefix = prefix
            self._inject_mode = "native_prefix"
            self.logger.info(
                f"Injected BGP missing route on {params.host_name} "
                f"({ops.profile} withdrew network {prefix})."
            )
            return
        prefixes = ops.bgp_networks()
        if not prefixes:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: no BGP network statements on "
                f"{params.host_name!r} to withdraw ({ops.profile})."
            )
        for prefix in prefixes:
            ops.withdraw_bgp_network(prefix)
        self._inject_mode = "native_bgp_network"
        if len(prefixes) == 1:
            self._withdrawn_prefix = prefixes[0]
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"({ops.profile} withdrew BGP networks {prefixes})."
        )

    def _inject_missing_adv_prefix(
        self, params: BGPMissingAdvertiseParams, prefix: str
    ) -> None:
        prefix = str(ipaddress.ip_network(prefix, strict=False))
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        cmd = frr_config_command(
            f"router bgp {asn}",
            f"no network {prefix}",
            "address-family ipv4 unicast",
            f"no network {prefix}",
        )
        self.runtime.exec(params.host_name, cmd)
        frr_save_config(self.runtime, params.host_name)
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
        lines = [f"router bgp {asn}"]
        for prefix in prefixes:
            lines += [f"no network {prefix}"]
        lines.append("address-family ipv4 unicast")
        for prefix in prefixes:
            lines.append(f"no network {prefix}")
        self.runtime.exec(params.host_name, frr_config_command(*lines))
        frr_save_config(self.runtime, params.host_name)
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

    def _redistributing_vrfs(self, host: str) -> list[str]:
        """BGP VRF instances on *host* that redistribute connected routes."""
        config = self.runtime.exec(host, "vtysh -c 'show running-config'")
        vrfs: list[str] = []
        vrf = None
        for line in config.splitlines():
            if not line.startswith(" "):
                header = re.match(r"router bgp \d+ vrf (\S+)", line)
                vrf = header.group(1) if header else None
            elif vrf and line.strip() == "redistribute connected" and vrf not in vrfs:
                vrfs.append(vrf)
        return vrfs

    def _inject_missing_adv_enterprise_redistribute(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        # Overlay VRFs advertise LAN prefixes via redistribute connected. Edit
        # only the VRF instances the router has: entering a missing one would
        # create an empty instance that was not there before the fault.
        vrfs = self._redistributing_vrfs(params.host_name)
        if not vrfs:
            raise RuntimeCapabilityError(
                f"{params.host_name!r} has no BGP VRF that redistributes "
                "connected routes."
            )
        lines: list[str] = []
        for vrf in vrfs:
            lines += [
                f"router bgp {asn} vrf {vrf}",
                "address-family ipv4 unicast",
                "no redistribute connected",
                "exit",
                "exit",
            ]
        cmd = frr_config_command(*lines)
        self.runtime.exec(params.host_name, f"{cmd} 2>/dev/null || true")
        frr_save_config(self.runtime, params.host_name)
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
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            return self._verify_missing_adv_native(params, ops)
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

    def _verify_missing_adv_native(
        self, params: BGPMissingAdvertiseParams, ops: RouterFaultOps
    ) -> dict:
        mode = self._inject_mode or (
            "native_prefix" if params.prefix else "native_bgp_network"
        )
        prefix = params.prefix or self._withdrawn_prefix
        if mode == "native_prefix" and prefix:
            prefix = str(ipaddress.ip_network(prefix, strict=False))
            verified = prefix not in set(ops.bgp_networks())
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "mode": mode,
                    "prefix": prefix,
                },
            )
        networks = ops.bgp_networks()
        verified = len(networks) == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "mode": mode,
                "bgp_network_count": len(networks),
                "networks": networks,
            },
        )


# ==================================================================
""" Problem: BGP blackhole community leak (remote-triggered blackholing). """
# ==================================================================


# Operator-style name for the customer prefix the misapplied policy matches.
RTBH_PREFIX_LIST = "CUSTOMER-PREFIXES"


def _profile_leaker(problem: ProblemBase, bgp: dict, host_name: str) -> str:
    """Return the profile leaker; ``host_name`` must name it so GT matches inject."""
    leaker = str(bgp.get("leaker_device") or host_name)
    if host_name != leaker:
        raise RuntimeCapabilityError(
            f"{type(problem).__name__}: host_name={host_name!r} must be the "
            f"profile leaker_device {leaker!r}."
        )
    return leaker


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
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    root_cause_name: str = "bgp_blackhole_community_leak"
    description = "A provider blackhole BGP community is leaked on export."
    TAGS: str = ["bgp", "isp", "ebgp", "rtbh"]
    COMPATIBLE_COLUMNS = frozenset({"isp_abilene_ebgp_rtbh", "isp_dfn-bwin_ebgp_rtbh"})

    Params = BGPBlackholeCommunityLeakParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = loaded_inventory(ctx.net_env)
        bgp_inv = inventory.get("bgp")
        if not isinstance(bgp_inv, dict) or not bgp_inv.get("rtbh"):
            _plan, bgp_inv = _compile_isp_bgp_inventory(
                ctx.scenario, igp="ospf", bgp_mode="ebgp", rtbh=True
            )
        return dict(isp_inject_params(ctx.problem, inventory, bgp_inv))

    symptom_desc = (
        "Reachability to a legitimately originated business prefix is lost while "
        "BGP sessions remain up."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPBlackholeCommunityLeakParams):
        bgp = self._rtbh_bgp_inventory()
        return [node_resource(_profile_leaker(self, bgp, params.host_name))]

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
        leaker = _profile_leaker(self, bgp, params.host_name)
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        neighbor_ip = str(bgp.get("leaker_to_rtbh_neighbor_ip") or "")
        if not target_prefix or not community or not route_map or not neighbor_ip:
            raise RuntimeCapabilityError(
                f"{type(self).__name__} missing RTBH inventory fields."
            )
        cmd = frr_config_command(
            f"ip prefix-list {RTBH_PREFIX_LIST} seq 5 permit {target_prefix} le 24",
            f"route-map {route_map} permit 5",
            f"match ip address prefix-list {RTBH_PREFIX_LIST}",
            f"set community {community} additive",
        )
        self.runtime.exec(leaker, cmd)
        frr_save_config(self.runtime, leaker)
        self.runtime.exec(
            leaker,
            f"vtysh -c 'clear ip bgp {neighbor_ip} soft out' 2>/dev/null || true",
        )
        provider = str(bgp.get("rtbh_provider_device") or "")
        if provider:
            wait_until(
                lambda: self._community_present(provider, target_prefix, community),
                20,
            )
        self.logger.info(
            f"Injected BGP blackhole community leak on {leaker} toward "
            f"{neighbor_ip} for {target_prefix} (community {community})."
        )

    def _bgp_sessions_established(self, devices: list[str]) -> bool:
        from nika.net_env.verify import frr_bgp_established_peers

        return all(
            frr_bgp_established_peers(
                self.runtime.exec(
                    device, "vtysh -c 'show bgp summary json' 2>/dev/null || true"
                )
            )
            for device in devices
        )

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

    def verify_fault(self, params: BGPBlackholeCommunityLeakParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
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
        # Data-plane impact is a symptom; src/nika/validation/symptom probes it.
        sessions_ok = self._bgp_sessions_established(
            [d for d in (leaker, origin_device, provider) if d]
        )

        verified = origin_ok and community_ok and rtbh_ok and sessions_ok
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
        leaker = _profile_leaker(self, bgp, params.host_name)
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        neighbor_ip = str(bgp.get("leaker_to_rtbh_neighbor_ip") or "")
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        provider = str(bgp.get("rtbh_provider_device") or "")
        ping_addr = params.probe_dst_ip or str(bgp.get("target_ping_address") or "")
        observer = params.symptom_host or str(bgp.get("data_plane_observer_host") or "")
        discard_nh = str(bgp.get("discard_next_hop") or "")

        cmd = frr_config_command(
            f"no route-map {route_map} permit 5",
            f"no ip prefix-list {RTBH_PREFIX_LIST} seq 5",
        )
        self.runtime.exec(leaker, cmd)
        frr_save_config(self.runtime, leaker)
        self.runtime.exec(
            leaker,
            f"vtysh -c 'clear ip bgp {neighbor_ip} soft out' 2>/dev/null || true",
        )
        wait_until(
            lambda: (
                not self._community_present(provider, target_prefix, community)
                and not self._rtbh_forwarding_active(
                    provider, target_prefix, discard_nh
                )
            ),
            20,
        )

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
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_rpki_invalid_route_leak"
    description = "RPKI-invalid prefixes are leaked into BGP."
    TAGS: str = ["rpki"]
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": True}

    Params = BGPRPKIInvalidRouteLeakParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = loaded_inventory(ctx.net_env)
        bgp_inv = inventory.get("bgp")
        if not isinstance(bgp_inv, dict) or not bgp_inv.get("rpki"):
            isp_opts = ctx.isp_options or isp_protocol_for(cls, {"rpki"})
            _plan, bgp_inv = _compile_isp_bgp_inventory(
                ctx.scenario,
                igp=str(isp_opts.get("igp") or "ospf"),
                bgp_mode=str(isp_opts.get("bgp_mode") or "ebgp"),
                rpki=True,
            )
        return dict(isp_inject_params(ctx.problem, inventory, bgp_inv))

    symptom_desc = (
        "Some BGP speakers learn RPKI-invalid routes from a leaked origin while "
        "ROV-enabled peers reject them."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPRPKIInvalidRouteLeakParams):
        return [
            node_resource(device)
            for device in self._leaker_devices(self._rpki_bgp_inventory(), params)
        ]

    def _leaker_devices(
        self, bgp: dict, params: BGPRPKIInvalidRouteLeakParams
    ) -> list[str]:
        """The border router whose eBGP export policy inject rewrites.

        Only its sessions announce the leak; the rest of the leaker AS keeps
        its healthy export policy.
        """
        return [_profile_leaker(self, bgp, params.host_name)]

    def _prefix_learned(self, device: str, prefix: str, origin_asn: int) -> bool:
        """Whether ``device`` holds a path for ``prefix`` originated by ``origin_asn``."""
        out = self.runtime.exec(
            device,
            f"vtysh -c 'show bgp ipv4 unicast {prefix} json' 2>/dev/null || true",
        )
        try:
            paths = (json.loads(out or "{}") or {}).get("paths") or []
        except ValueError:
            return False
        for path in paths:
            tokens = str((path.get("aspath") or {}).get("string") or "").split()
            if tokens and tokens[-1] == str(origin_asn):
                return True
        return False

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
        leaker = _profile_leaker(self, bgp, params.host_name)
        devices = self._leaker_devices(bgp, params)
        # Permit the export-deny prefixes on its eBGP export (replaces the
        # healthy deny seq 5).
        for device in devices:
            cmd = frr_config_command(
                "route-map BGP-OUT permit 5",
                f"match ip address prefix-list {EXPORT_DENY_PREFIX_LIST}",
            )
            self.runtime.exec(device, cmd)
            frr_save_config(self.runtime, device)
            self.runtime.exec(
                device, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
        # Wait for eBGP export to reach the non-ROV observer before verify_fault.
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        non_rov = str(bgp.get("non_rov_observer") or "")
        leaker_asn = int(bgp.get("leaker_asn") or 0)
        if non_rov and prefixes:
            wait_until(
                lambda: all(
                    self._prefix_learned(non_rov, p, leaker_asn) for p in prefixes
                ),
                25,
            )
        self.logger.info(
            f"Injected RPKI-invalid route leak via incorrect BGP export policy "
            f"on {leaker}."
        )

    def verify_fault(self, params: BGPRPKIInvalidRouteLeakParams) -> dict:
        """Verify the leaker AS's live export policy now sends the leak prefixes.

        Reads only the leaker side: the running ``BGP-OUT`` permit on every
        leaker-AS router and the eBGP peers the leaker advertises each prefix
        to. Which remote ASes accept or reject the leak is the symptom.
        """
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        devices = self._leaker_devices(bgp, params)
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        ebgp_peers = {
            str(s.get("remote_ip"))
            for s in bgp.get("sessions") or []
            if s.get("local_device") == leaker and s.get("session_type") == "ebgp"
        }
        export_permit = {
            device: "route-map BGP-OUT permit 5"
            in self.runtime.exec(
                device, "vtysh -c 'show running-config' 2>/dev/null || true"
            )
            for device in devices
        }
        advertised_to: dict[str, list[str]] = {}
        for prefix in prefixes:
            out = self.runtime.exec(
                leaker,
                f"vtysh -c 'show bgp ipv4 unicast {prefix} json' 2>/dev/null || true",
            )
            try:
                targets = set((json.loads(out) or {}).get("advertisedTo") or {})
            except ValueError:
                targets = set()
            advertised_to[prefix] = sorted(targets & ebgp_peers)
        verified = (
            bool(prefixes)
            and all(export_permit.values())
            and all(advertised_to.values())
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "leaker_device": leaker,
                "leak_prefixes": prefixes,
                "export_permit": export_permit,
                "advertised_to_ebgp_peers": advertised_to,
            },
        )

    def _leak_absent(
        self, observers: list[str], prefixes: list[str], leaker_asn: int
    ) -> bool:
        return not any(
            self._prefix_learned(observer, prefix, leaker_asn)
            for observer in observers
            for prefix in prefixes
        )

    def recover_fault(self, params: BGPRPKIInvalidRouteLeakParams) -> dict:
        """Restore healthy BGP-OUT deny for leak prefixes on the leaker AS."""
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot recover_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        devices = self._leaker_devices(bgp, params)
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        observers = [
            str(bgp.get("rov_observer") or ""),
            str(bgp.get("non_rov_observer") or ""),
        ]
        observers = [o for o in observers if o]

        for device in devices:
            cmd = frr_config_command(
                "route-map BGP-OUT deny 5",
                f"match ip address prefix-list {EXPORT_DENY_PREFIX_LIST}",
            )
            self.runtime.exec(device, cmd)
            frr_save_config(self.runtime, device)
            self.runtime.exec(
                device, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
        leaker_asn = int(bgp.get("leaker_asn") or 0)
        wait_until(lambda: self._leak_absent(observers, prefixes, leaker_asn), 25)
        leak_absent = self._leak_absent(observers, prefixes, leaker_asn)

        ok = leak_absent
        details = {
            "host": params.host_name,
            "leaker_device": leaker,
            "recovered_devices": devices,
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
# Operator-style name for the de-aggregated more-specifics the peer advertises.
# The policy goes to every BGP router, as a fleet-wide push would, so it does
# not single out the two routers of the faulty session.
_FLOOD_PREFIX_LIST = "TE-MORE-SPECIFICS"


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
    root_cause_resource_match = "any"
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_max_prefix_exceeded"
    description = "BGP maximum-prefix limit is exceeded on a session."
    TAGS: str = ["bgp", "isp"]
    COMPATIBLE_COLUMNS = frozenset({"isp_abilene/ebgp", "isp_geant/ebgp"})
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}

    Params = BGPMaxPrefixExceededParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        """The session that carries the customer site's route (its only path)."""
        from nika.net_env.isp.inject_targets import customer_route_session

        bgp_inv = loaded_inventory(ctx.net_env).get("bgp")
        if not isinstance(bgp_inv, dict) or "customer_site" not in bgp_inv:
            isp_opts = ctx.isp_options or isp_protocol_for(cls, {"bgp"})
            _plan, bgp_inv = _compile_isp_bgp_inventory(
                ctx.scenario,
                igp=str(isp_opts.get("igp") or "ospf"),
                bgp_mode=str(isp_opts.get("bgp_mode") or "ebgp"),
            )
        params = dict(customer_route_session(bgp_inv))
        params["flood_count"] = "120"
        return params

    symptom_desc = (
        "An eBGP peer advertises more prefixes than the receiver's configured "
        "maximum-prefix, resetting the session and withdrawing learned routes; "
        "a customer route learned only over that session leaves the receiver's "
        "AS, and the customer loses its service there."
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
            "isp with bgp_mode=ebgp."
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
            router, "vtysh -c 'show bgp summary json' 2>/dev/null || true"
        )
        try:
            peers = json.loads(summary).get("ipv4Unicast", {}).get("peers", {})
            return int(peers.get(neighbor_ip, {}).get("pfxRcd", 0))
        except (ValueError, TypeError, AttributeError):
            return 0

    def customer_route(self, receiver: str, peer: str) -> dict | None:
        """The scenario's customer route that ``receiver`` learns only from ``peer``."""
        site = self._bgp_inventory().get("customer_site")
        if (
            isinstance(site, dict)
            and site.get("partner") == receiver
            and site.get("provider") == peer
        ):
            return site
        return None

    def _has_route(self, router: str, prefix: str) -> bool:
        out = self.runtime.exec(
            router, f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true"
        )
        return "Network not in table" not in out and prefix.split("/")[0] in out

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

    def _bgp_routers(self, receiver: str, peer: str) -> list[str]:
        """Every router with a BGP session in the inventory."""
        routers = {
            str(s.get("local_device") or "")
            for s in self._bgp_inventory().get("sessions") or []
        }
        return sorted({receiver, peer, *routers} - {""})

    def _flood_policy_maps(self, device: str) -> list[str]:
        config = self.runtime.exec(device, "vtysh -c 'show running-config'")
        maps = sorted(
            set(
                re.findall(
                    r"^\s*neighbor \S+ route-map ([A-Za-z0-9_.-]+) (?:in|out)$",
                    config,
                    re.M,
                )
            )
        )
        if not maps:
            raise RuntimeCapabilityError(
                "BGP flood setup requires bound import/export policies"
            )
        return maps

    def _install_flood_policy(self, device: str) -> None:
        # Include solved import policies, not only the baseline policy names.
        lines = [
            f"ip prefix-list {_FLOOD_PREFIX_LIST} seq 5 permit 198.19.0.0/16 le 24"
        ]
        for policy in self._flood_policy_maps(device):
            lines += [
                f"route-map {policy} permit 1",
                f"match ip address prefix-list {_FLOOD_PREFIX_LIST}",
            ]
        self.runtime.exec(device, frr_config_command(*lines))

    def _remove_flood_policy(self, device: str) -> None:
        lines = [
            f"no route-map {policy} permit 1"
            for policy in self._flood_policy_maps(device)
        ]
        lines.append(f"no ip prefix-list {_FLOOD_PREFIX_LIST}")
        self.runtime.exec(device, frr_config_command(*lines))

    def _flood_prefixes(self, count: int, *, peer: str, neighbor_ip: str) -> list[str]:
        """Pick ``count`` /24s scattered within the flood /16, not a tidy block.

        Real de-aggregation/leak incidents (e.g. Optus 2023) announce many
        non-contiguous more-specifics, not one ascending sequential range.
        The selection is deterministic per session (seeded from the peer and
        neighbor address) so repeated runs of the same case see the same
        flood set.
        """
        rng = random.Random(f"{peer}|{neighbor_ip}")
        octets = list(range(256))
        rng.shuffle(octets)
        count = min(count, 256)
        return sorted(f"{_FLOOD_PREFIX_BASE}.{octet}.0/24" for octet in octets[:count])

    def _advertise_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        lines: list[str] = []
        config = self.runtime.exec(peer, "vtysh -c 'show running-config'")
        if "no bgp network import-check" not in config:
            # FRR originates a ``network`` only when a matching route exists,
            # so back the announcements with discard routes under import check.
            lines.extend(f"ip route {prefix} Null0" for prefix in prefixes)
        lines += [f"router bgp {asn}", "address-family ipv4 unicast"]
        lines.extend(f"network {prefix}" for prefix in prefixes)
        self.runtime.exec(peer, frr_config_command(*lines))
        frr_save_config(self.runtime, peer)
        self.runtime.exec(
            peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
        )

    def _withdraw_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        lines = [f"router bgp {asn}", "address-family ipv4 unicast"]
        lines.extend(f"no network {prefix}" for prefix in prefixes)
        config = self.runtime.exec(peer, "vtysh -c 'show running-config'")
        statics = [p for p in prefixes if f"ip route {p} Null0" in config]
        if statics:
            lines += ["exit-address-family", "exit"]
            lines.extend(f"no ip route {prefix} Null0" for prefix in statics)
        self.runtime.exec(peer, frr_config_command(*lines))
        frr_save_config(self.runtime, peer)

    def _set_maximum_prefix(
        self, receiver: str, asn: int, neighbor_ip: str, limit: int
    ) -> None:
        cmd = frr_config_command(
            f"router bgp {asn}",
            "address-family ipv4 unicast",
            f"neighbor {neighbor_ip} maximum-prefix {limit}",
        )
        self.runtime.exec(receiver, cmd)
        frr_save_config(self.runtime, receiver)

    def _clear_maximum_prefix(self, receiver: str, asn: int, neighbor_ip: str) -> None:
        cmd = frr_config_command(
            f"router bgp {asn}",
            "address-family ipv4 unicast",
            f"no neighbor {neighbor_ip} maximum-prefix",
        )
        self.runtime.exec(receiver, cmd)
        frr_save_config(self.runtime, receiver)

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
        prefixes = self._flood_prefixes(flood_count, peer=peer, neighbor_ip=neighbor_ip)

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

        bounded_parallel_map(
            self._install_flood_policy, self._bgp_routers(receiver, peer)
        )
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
        """Verify the live limit, the peer's larger prefix set, and the torn-down session.

        The limit and the flood are read from both routers' running configs;
        the receiver must hold the session down for the exceeded limit.
        """
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        receiver, peer, neighbor_ip = self._resolve_session(params)
        receiver_cfg = self.runtime.exec(
            receiver, "vtysh -c 'show running-config' 2>/dev/null || true"
        )
        match = re.search(
            rf"neighbor {re.escape(neighbor_ip)} maximum-prefix (\d+)", receiver_cfg
        )
        limit = int(match.group(1)) if match else None
        flood = len(self._discover_flood_prefixes(peer))
        neighbor = self._neighbor_output(receiver, neighbor_ip)
        session_down = not self._session_established(receiver, neighbor_ip)
        # FRR prints this only for a session it shut for an exceeded limit.
        held_for_limit = (
            "peer had exceeded the max. no. of prefixes" in neighbor.lower()
        )
        verified = (
            limit is not None and flood > limit and session_down and held_for_limit
        )
        details = {
            "receiver": receiver,
            "peer": peer,
            "neighbor_ip": neighbor_ip,
            "maximum_prefix": limit,
            "peer_flood_prefixes": flood,
            "session_down": session_down,
            "held_down_for_limit": held_for_limit,
        }
        site = self.customer_route(receiver, peer)
        if site is not None:
            # The customer route had no other way into the receiver's AS.
            stranded = not self._has_route(receiver, str(site["prefix"]))
            details["customer_prefix"] = site["prefix"]
            details["customer_route_withdrawn"] = stranded
            verified = verified and stranded
        return build_verify_result(
            fault_type=self.root_cause_name, verified=verified, details=details
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
            prefixes = self._flood_prefixes(
                max(params.flood_count, 80), peer=peer, neighbor_ip=neighbor_ip
            )
        receiver_asn = int(
            state.get("receiver_asn") or self.runtime.frr_get_bgp_asn_number(receiver)
        )
        peer_asn = int(
            state.get("peer_asn") or self.runtime.frr_get_bgp_asn_number(peer)
        )

        self._withdraw_flood(peer, peer_asn, prefixes)
        bounded_parallel_map(
            self._remove_flood_policy, self._bgp_routers(receiver, peer)
        )
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
        site = self.customer_route(receiver, peer)

        def routes_back() -> bool:
            if site is not None and not self._has_route(receiver, str(site["prefix"])):
                return False
            return not self._business_prefix_missing(receiver, peer)

        routes_ok = routes_back()
        # Business routes may need a short extra settle after session up.
        if session_up and not routes_ok:
            routes_ok = wait_until(routes_back, 15)
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
