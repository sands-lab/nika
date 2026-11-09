"""Business VRF import-policy failures on enterprise Site Edges."""

from __future__ import annotations

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from nika.net_env.enterprise_branch.addressing import lan_edge_ip, lan_host_ip
from nika.net_env.enterprise_branch.topology import OVERLAY_ROLES, PUBLIC_SERVICE_IP
from nika.net_env.utils.routeros.parse import routeros_terse_rows
from nika.net_env.verify import frr_bgp_established_peers, ping_stats
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.polling import wait_until
from nika.problems.support.oracle import Check, OraclePlaybook, routeros, shell, vtysh
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.runtime.base import RuntimeCapabilityError


def _frr_imports(config: str, vrf: str) -> set[str]:
    """Read IPv4 import sources from one BGP VRF, excluding other instances."""
    block = re.search(
        rf"^router bgp \d+ vrf {re.escape(vrf)}\n(.*?)(?=^\S|\Z)",
        config,
        re.M | re.S,
    )
    family = re.search(
        r"^ address-family ipv4 unicast\n(.*?)^ exit-address-family",
        block[1] if block else "",
        re.M | re.S,
    )
    return set(re.findall(r"^  import vrf (\S+)$", family[1] if family else "", re.M))


def _site_lan(net_env, host: str, vrf: str):
    edge = next((e for e in net_env._edges.values() if e.name == host), None)
    if edge is None:
        raise ValueError(f"{host!r} is not a Site Edge")
    site = net_env.spec.sites[edge.site]
    lan = next(
        (lan for lan in site.lans if lan.vrf == vrf and lan.role in OVERLAY_ROLES),
        None,
    )
    if lan is None:
        raise ValueError(f"{host}:{vrf} is not an overlay business VRF")
    return site, lan, edge


class VrfRouteTargetMismatchParams(BaseModel):
    host_name: str = Field(description="Target Site Edge.")
    vrf_name: Literal["vrf_corp", "vrf_server"] = Field(
        default="vrf_corp", description="Business VRF whose import target changes."
    )


class VrfRouteTargetMismatch(ProblemBase):
    """One business VRF imports from the wrong routing domain.

    RFC 4364 section 4.3.1 defines import/export target matching:
    https://www.rfc-editor.org/rfc/rfc4364.html#section-4.3.1
    MikroTik's vendor configuration document describes local BGP VPN leaking:
    https://help.mikrotik.com/docs/spaces/ROS/pages/328206/Virtual+Routing+and+Forwarding+-+VRF
    FRR derives route targets from the source named by ``import vrf``:
    https://docs.frrouting.org/en/latest/bgp.html#clicmd-import-vrf-VRFNAME

    The NHS Wales incident report describes a route-leaking change admitting
    an unintended default through customer VRF import target 64627:16:
    https://swanseabay.nhs.wales/about-us/our-health-board/our-board-business/health-board-committees/digital-data-research-and-innovation-committee/may-2026/4-1i-appendix-1-wales-wide-outage-pir-pdf/
    The NANOG operator discussion records VPN updates losing their RT community:
    https://seclists.org/nanog/2011/Mar/457
    These sources establish the operational impact of import changes and RT
    loss; neither describes this exact import-target typo.

    RouterOS imports a mistyped route target. FRR imports the guest VRF instead
    of the default VRF that carries overlay routes. Its unreachable VRF default
    prevents fallback to the main table after remote routes disappear.
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "vrf_route_target_mismatch"
    description = (
        "A business VRF's import source does not match the overlay routing domain."
    )
    symptom_desc = "Users at one site cannot reach remote business services."
    symptom_probe = "custom"
    symptom_class = "control_plane"
    effect_protocol = "bgp"
    TAGS = ["bgp"]
    COMPATIBLE_COLUMNS = frozenset({"enterprise_branch"})
    supported_backends = ("kathara",)
    native_router_operations = frozenset(
        {
            "vpn_import_targets",
            "set_vpn_import_targets",
            "vrf_routes",
            "bgp_established_peers",
            "tunnel_peer",
        }
    )
    BENCHMARK_TARGETS = "canonical"
    Params = VrfRouteTargetMismatchParams

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        return device_profile == "frr" or super().runs_on_device_profile(device_profile)

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = sorted(
            edge.name
            for edge in ctx.net_env._edges.values()
            if not ctx.net_env.spec.sites[edge.site].is_hub
            and native_router_profile(ctx.net_env, edge.name) in {None, "routeros"}
        )
        return {
            "host_name": ctx.rng.choice(targets) if targets else ctx.router0,
            "vrf_name": "vrf_corp",
        }

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        host = inject.get("host_name")
        if native_router_profile(ctx.net_env, host) not in {None, "routeros"}:
            raise ValueError("VRF import mismatch needs an FRR or RouterOS Site Edge")
        _site_lan(ctx.net_env, host, inject.get("vrf_name", "vrf_corp"))

    def root_cause_resources(self, params):
        return [node_resource(params.host_name)]

    def _require_target(self, params):
        if native_router_profile(self.net_env, params.host_name) not in {
            None,
            "routeros",
        }:
            raise RuntimeCapabilityError(
                "VRF import mismatch requires an FRR or RouterOS Site Edge."
            )
        _site_lan(self.net_env, params.host_name, params.vrf_name)

    def _import_sources(self, params):
        if native_router_profile(self.net_env, params.host_name) == "routeros":
            site, _, _ = _site_lan(self.net_env, params.host_name, params.vrf_name)
            return f"{site.asn}:1", f"{site.asn}:2"
        return "default", "vrf_guest"

    def _imports(self, params):
        ops = native_fault_ops(self, params.host_name, "vpn_import_targets")
        if ops:
            return ops.vpn_import_targets(params.vrf_name)
        return _frr_imports(
            self.runtime.exec(params.host_name, vtysh("show running-config")),
            params.vrf_name,
        )

    def _set_imports(self, params, imports):
        ops = native_fault_ops(self, params.host_name, "set_vpn_import_targets")
        if ops:
            ops.set_vpn_import_targets(params.vrf_name, imports)
            return
        current = self._imports(params)
        site, _, _ = _site_lan(self.net_env, params.host_name, params.vrf_name)
        output = self.runtime.exec(
            params.host_name,
            frr_config_command(
                f"router bgp {site.asn} vrf {params.vrf_name}",
                "address-family ipv4 unicast",
                *(f"no import vrf {vrf}" for vrf in sorted(current - imports)),
                *(f"import vrf {vrf}" for vrf in sorted(imports - current)),
                "exit-address-family",
            ),
        )
        if "%" in output:
            raise RuntimeError(f"FRR rejected the VRF import update: {output}")
        frr_save_config(self.runtime, params.host_name)

    def probe_paths(self, params):
        site, lan, _ = _site_lan(self.net_env, params.host_name, params.vrf_name)
        remote = next(
            s
            for s in self.net_env.spec.sites.values()
            if s.name != site.name and s.is_hub
        )
        destination = next(
            candidate for candidate in remote.lans if candidate.role == "server"
        )
        guest = next(candidate for candidate in site.lans if candidate.role == "guest")
        paths = {
            "remote": (
                lan.host_name,
                str(lan_host_ip(remote.site_id, destination.role).ip),
            ),
            "gateway": (lan.host_name, str(lan_edge_ip(site.site_id, lan.role).ip)),
            "guest": (guest.host_name, PUBLIC_SERVICE_IP),
        }
        for other in site.lans:
            if other.role in OVERLAY_ROLES and other != lan:
                paths[other.vrf] = (other.host_name, paths["remote"][1])
                paths[f"local_{other.vrf}"] = (
                    lan.host_name,
                    str(lan_host_ip(site.site_id, other.role).ip),
                )
        return paths

    def path_readings(self, params):
        return {
            name: ping_stats(self.runtime, src, dst, count=4).received
            for name, (src, dst) in self.probe_paths(params).items()
        }

    def routing_readings(self, params):
        ops = native_fault_ops(self, params.host_name, "vrf_routes")
        _, _, edge = _site_lan(self.net_env, params.host_name, params.vrf_name)
        if ops is None:
            output = self.runtime.exec(
                params.host_name,
                vtysh(
                    "show bgp summary json",
                    "show ip route json",
                    f"show ip route vrf {params.vrf_name} json",
                )
                + "; wg show all latest-handshakes",
            )
            tables = []
            for _ in range(3):
                table, end = json.JSONDecoder().raw_decode(output.lstrip())
                tables.append(table)
                output = output.lstrip()[end:]
            handshakes = {
                fields[0]: time.time() - int(fields[2]) if int(fields[2]) else None
                for line in output.splitlines()
                if len(fields := line.split()) == 3 and fields[0] in edge.tunnels
            }
            readings = {
                "peers": sorted(frr_bgp_established_peers(json.dumps(tables[0])))
            }
            for name, table in zip(("overlay", "vrf"), tables[1:], strict=True):
                readings[name] = sorted(
                    prefix
                    for prefix, routes in table.items()
                    if any(
                        route.get("selected")
                        and not any(
                            nh.get("unreachable") for nh in route.get("nexthops", [])
                        )
                        for route in routes
                    )
                )
            readings["handshakes"] = {
                iface: handshakes.get(iface) for iface in edge.tunnels
            }
            return readings
        return {
            "peers": sorted(ops.bgp_established_peers()),
            "overlay": sorted(ops.vrf_routes("overlay")),
            "vrf": sorted(ops.vrf_routes(params.vrf_name)),
            "handshakes": {
                iface: ops.tunnel_peer(iface).handshake_age_sec
                for iface in edge.tunnels
            },
        }

    def inject_fault(self, params):
        self._require_target(params)
        imports = self._imports(params)
        overlay_target, wrong_target = self._import_sources(params)
        if overlay_target not in imports:
            raise RuntimeCapabilityError(
                "Business VRF does not import the overlay target"
            )
        self._healthy_imports = imports
        self._healthy_routing = self.routing_readings(params)
        if not self._healthy_routing["peers"] or not self._healthy_routing["overlay"]:
            raise RuntimeCapabilityError(
                "VRF import mismatch needs healthy overlay routing"
            )
        wrong_imports = (imports - {overlay_target}) | {wrong_target}
        self._set_imports(params, wrong_imports)

    def verify_fault(self, params):
        self._require_target(params)
        imports = self._imports(params)
        overlay_target, wrong_target = self._import_sources(params)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=overlay_target not in imports and wrong_target in imports,
            details={
                "host": params.host_name,
                "vrf": params.vrf_name,
                "imports": sorted(imports),
            },
        )

    def oracle_playbook(self, params):
        _, lan, _ = _site_lan(self.net_env, params.host_name, params.vrf_name)
        remote = self.probe_paths(params)["remote"][1]
        prefix = remote.rsplit(".", 1)[0] + ".0/24"

        if native_router_profile(self.net_env, params.host_name) is None:
            return OraclePlaybook(
                finding=f"{params.host_name}:{lan.vrf} imports vrf_guest instead of the overlay's default VRF.",
                steps=[
                    shell(
                        params.host_name,
                        vtysh("show running-config")
                        + f" | sed -n '/^router bgp [0-9]* vrf {lan.vrf}$/,/^exit$/p'; "
                        + vtysh(
                            "show bgp summary",
                            f"show ip route vrf {lan.vrf} {prefix}",
                            f"show ip route {prefix}",
                        )
                        + "; wg show all latest-handshakes",
                        "Compare the business import source with the overlay VRF and inspect sessions, routes, and tunnels.",
                        Check(
                            "the business VRF imports vrf_guest and has no import from default",
                            lambda output: (
                                "vrf_guest" in _frr_imports(output, lan.vrf)
                                and "default" not in _frr_imports(output, lan.vrf)
                            ),
                        ),
                        evidence=True,
                    ),
                    shell(
                        lan.host_name,
                        f"ping -c 4 -W 2 {remote}; ping -c 4 -W 2 {self.probe_paths(params)['gateway'][1]}",
                        "Compare remote service reachability with the local gateway.",
                        Check(
                            "the remote service loses all four probes and the local gateway answers four",
                            lambda output: (
                                "4 packets transmitted, 0 received" in output
                                and "4 packets transmitted, 4 received" in output
                            ),
                        ),
                    ),
                ],
            )

        def mismatch(output):
            rows = routeros_terse_rows(output)
            business = next((r for r in rows if r.get("vrf") == lan.vrf), {})
            overlay = next((r for r in rows if r.get("vrf") == "overlay"), {})
            imports = set(business.get("import.route-targets", "").split(",")) - {""}
            exports = set(overlay.get("export.route-targets", "").split(",")) - {""}
            return bool(imports and exports and not imports & exports)

        return OraclePlaybook(
            finding=f"{params.host_name}:{lan.vrf} imports no target exported by the overlay VRF.",
            steps=[
                routeros(
                    params.host_name,
                    f'/routing/bgp/vpn/print terse where vrf="{lan.vrf}" or vrf="overlay"; '
                    "/routing/bgp/session/print terse where established=yes; "
                    f'/ip/route/print detail where routing-table="{lan.vrf}" and dst-address="{prefix}"; '
                    f'/ip/route/print detail where routing-table="overlay" and dst-address="{prefix}"; '
                    "/interface/wireguard/peers/print terse",
                    "Compare business imports with overlay exports and inspect sessions, routes, and tunnels.",
                    Check(
                        "the business import targets and overlay export targets are disjoint",
                        mismatch,
                    ),
                    evidence=True,
                )
            ],
        )

    def recover_fault(self, params):
        self._require_target(params)
        imports = getattr(self, "_healthy_imports", None)
        baseline = getattr(self, "_healthy_routing", None)
        if not imports or not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        self._set_imports(params, imports)

        def healthy():
            self._recovered_routing = self.routing_readings(params)
            self._recovered_paths = self.path_readings(params)
            return (
                set(baseline["vrf"]) <= set(self._recovered_routing["vrf"])
                and set(baseline["peers"]) <= set(self._recovered_routing["peers"])
                and set(baseline["overlay"]) <= set(self._recovered_routing["overlay"])
                and all(
                    age is not None and age < 180
                    for age in self._recovered_routing["handshakes"].values()
                )
                and all(n == 4 for n in self._recovered_paths.values())
            )

        restored = wait_until(healthy, 120, interval_s=3)
        return {
            "verified": restored,
            "details": {
                "paths_received": self._recovered_paths,
                **self._recovered_routing,
            },
        }
