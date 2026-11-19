import ipaddress
import json
import re
import shlex
from dataclasses import replace
from typing import Literal, Optional

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.routing_control_plane.bgp import RPKIInvalidOrigin
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.frr import (
    FRR_CONF,
    FRR_DIR,
    frr_config_command,
    frr_save_config,
)
from nika.problems.support.oracle import (
    Check,
    compare,
    router_cli,
    OraclePlaybook,
    all_of,
    contains,
    lacks,
    shell,
    srl,
    srl_cli,
    vtysh,
)
from nika.problems.support.benchmark_targets import (
    access_router_victim,
    require_role_member,
)
from nika.net_env.utils.kathara.frr_origin import (
    BGP_ORIGIN_IFACE,
    ENSURE_BGP_ORIGIN_IFACE_CMD,
)
from nika.problems.support.recovery import FileSnapshot, checked_exec
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.service.containerlab.srl_api import srl_json, srl_bgp_prefix_paths
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==================================================================
# Problem: BGP hijacking problem.
# ==================================================================


def hijack_prefix_permissions(
    config: str, prefix: str, explicit: str | None = None
) -> list[tuple[str, int]]:
    """Missing permit entries in bound allow-lists that cover the hijacked prefix."""
    target = ipaddress.IPv4Network(prefix)
    # A route-map can contain several terms and call another policy.
    bound = set(re.findall(r"neighbor \S+ route-map (\S+) (?:in|out)", config))
    pending = list(bound)
    while pending:
        policy = pending.pop()
        bodies = re.findall(
            rf"^route-map {re.escape(policy)} (?:permit|deny) \d+\n((?:[ \t]+[^\n]*\n)*)",
            config,
            re.M,
        )
        for called in re.findall(r"\bcall (\S+)", "\n".join(bodies)):
            if called not in bound:
                bound.add(called)
                pending.append(called)
    names = set(re.findall(r"neighbor \S+ prefix-list (\S+) (?:in|out)", config))
    for policy in bound:
        for body in re.findall(
            rf"^route-map {re.escape(policy)} permit \d+\n((?:[ \t]+[^\n]*\n)*)",
            config,
            re.M,
        ):
            for match in re.findall(r"match ip address prefix-list ([^\n]+)", body):
                names.update(match.split())
    if explicit:
        names.add(explicit)
    result = []
    for name in sorted(names):
        rows = re.findall(
            rf"^ip prefix-list {re.escape(name)} seq (\d+) permit (\S+)([^\n]*)$",
            config,
            re.M,
        )
        covering = False
        permitted = False
        for _seq, network, qualifiers in rows:
            network = ipaddress.IPv4Network(network)
            if not target.subnet_of(network):
                continue
            covering = True
            ge = re.search(r"\bge (\d+)", qualifiers)
            le = re.search(r"\ble (\d+)", qualifiers)
            minimum = int(ge[1]) if ge else network.prefixlen
            maximum = int(le[1]) if le else 32 if ge else network.prefixlen
            permitted |= minimum <= target.prefixlen <= maximum
        if permitted or not (covering or name == explicit):
            continue
        used = {
            int(n)
            for n in re.findall(
                rf"^ip prefix-list {re.escape(name)} seq (\d+) ", config, re.M
            )
        }
        sequence = 1
        while sequence in used:
            sequence += 1
        result.append((name, sequence))
    return result


class BGPHijackingParams(BaseModel):
    """Parameters for injecting a BGP hijacking fault."""

    variant: Literal["prefix", "rpki_invalid_origin"] = "prefix"
    host_name: str = Field(description="Target router host name.")
    target_network: Optional[str] = Field(
        default=None,
        description="Network prefix to advertise. Defaults to runtime selection.",
    )
    symptom_host: Optional[str] = Field(
        default=None,
        description="Optional remote router that should learn the hijacked prefix.",
    )
    probe_dst_ip: Optional[str] = Field(
        default=None,
        description="Optional address inside the hijacked prefix for symptom probes.",
    )
    peer_host: Optional[str] = Field(
        default=None,
        description="Optional stub host attached to the hijacking router.",
    )
    observer_host: Optional[str] = Field(
        default=None,
        description=(
            "Optional end host routed through symptom_host whose traffic should "
            "follow the hijack. Defaults to the ISP stub host of symptom_host."
        ),
    )
    export_prefix_list: Optional[str] = Field(
        default=None,
        description=(
            "Optional prefix-list of the hijacker's outbound BGP policy that the "
            "misconfiguration also extends with the target, letting it out."
        ),
    )


def _bgp_paths(output: str) -> list[tuple[str, bool]]:
    """``(origin, best)`` per path of ``show bgp ipv4 unicast <prefix>`` output.

    The origin is ``AS<n>`` (last AS of a non-empty AS path) or, for a path
    from the local AS, the originator (or next hop) address.
    """
    paths = []
    for block in re.split(r"\n(?=  \S)", output):
        lines = block.splitlines()
        if len(lines) < 3 or not re.search(r"^\s+Origin ", block, re.M):
            continue
        as_path = [t for t in lines[0].split(",")[0].split() if t.isdigit()]
        if as_path:
            origin = f"AS{as_path[-1]}"
        else:
            originator = re.search(r"Originator: ([\d.]+)", block)
            hop = lines[1].split()[0] if lines[1].split() else ""
            # FRR appends the peer's hostname, as in 10.255.0.1(atlam5).
            hop = hop.split("(", 1)[0]
            origin = originator.group(1) if originator else hop
        best = bool(re.search(r"^\s+Origin [^\n]*\bbest\b", block, re.M))
        paths.append((origin, best))
    return paths


def _hijacker_ids(output: str) -> set[str]:
    m = re.search(r"^hijacker=(\S*)$", output, re.M)
    return set(m.group(1).split(",")) if m else set()


def _best_from_hijacker(output: str) -> bool:
    """The best path originates at the hijacker (``hijacker=`` line: its AS
    and addresses)."""
    ids = _hijacker_ids(output)
    return any(best and origin in ids for origin, best in _bgp_paths(output))


def _other_origin_seen(output: str) -> bool:
    """Besides its own, the hijacker holds a path from another origin (the
    owner): not its local AS and not one of its ``addr`` lines."""
    asn = re.search(r"local AS number (\d+)", output)
    own = {"0.0.0.0", *re.findall(r"^addr ([^/\s]+)", output, re.M)}
    if asn:
        own.add(f"AS{asn.group(1)}")
    return any(origin not in own for origin, _ in _bgp_paths(output))


def _srl_bgp_routes(data):
    """Iterate route records in an SR Linux JSON subtree."""
    if isinstance(data, dict):
        if "prefix" in data or "ipv4-prefix" in data:
            yield data
        for value in data.values():
            yield from _srl_bgp_routes(value)
    elif isinstance(data, list):
        for value in data:
            yield from _srl_bgp_routes(value)


def _scenario_targets(net_env) -> list:
    """Hijacks the scenario knows to win over the owner's route, if any."""
    targets = getattr(net_env, "bgp_hijack_targets", None)
    return list(targets()) if targets is not None else []


def _invalid_origin_available(net_env):
    bgp = (getattr(net_env, "inventory", None) or {}).get("bgp") or {}
    return bool(
        bgp.get("rpki")
        and any(
            row.get("local_device") == bgp.get("leaker_device")
            and row.get("remote_device") == bgp.get("non_rov_observer")
            and row.get("session_type") == "ebgp"
            for row in bgp.get("sessions", [])
        )
    )


def _target_params(target) -> dict[str, str]:
    return {
        "host_name": target.hijacker,
        "target_network": target.prefix,
        "symptom_host": target.observer,
        "observer_host": target.observer_host,
        "probe_dst_ip": target.probe_ip,
    }


def _hijack_config_state(config: str, target: str, export_list: str | None):
    """Configuration fields the FRR injector can change."""
    network = str(ipaddress.ip_network(target, strict=False))
    networks = tuple(
        re.findall(rf"^[ \t]+network {re.escape(network)}(?: .*)?$", config, re.M)
    )
    interface = re.search(
        rf"^interface {BGP_ORIGIN_IFACE}\n((?:[ \t]+[^\n]*\n)*)", config, re.M
    )
    addresses = (
        tuple(
            line
            for line in interface[1].splitlines()
            if line.strip().startswith("ip address ")
        )
        if interface
        else ()
    )
    export = (
        tuple(
            re.findall(
                rf"^ip prefix-list {re.escape(export_list)} seq \d+ .*$", config, re.M
            )
        )
        if export_list
        else ()
    )
    return networks, addresses, export


class BGPHijacking(RPKIInvalidOrigin, ProblemBase):
    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_hijacking"
    CONFUSABLE_WITH = (
        "bgp_blackhole_community_leak",
        "bgp_rov_not_enforced",
        "bgp_route_leak",
    )
    description = "An unauthorized BGP prefix is advertised (hijack)."
    TAGS: str = ["bgp"]
    native_router_operations = frozenset(
        {
            "bgp_advertised_to",
            "originate_bgp_prefix",
            "save_config",
            "restore_config",
            "bgp_networks",
        }
    )
    supported_backends = ("kathara", "containerlab")
    BENCHMARK_TARGET_ROLE = "access_routers"

    Params = BGPHijackingParams
    RECORDED_ATTRS = ("export_entry_snapshot",)

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = _scenario_targets(ctx.net_env)
        if targets:
            return _target_params(ctx.rng.choice(targets))
        return access_router_victim(ctx, prefer_site_edge=False)

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        targets = _scenario_targets(ctx.net_env)
        rows = (
            [_target_params(target) for target in targets]
            if targets
            else super().benchmark_inject_options(ctx, base)
        )
        bgp = (getattr(ctx.net_env, "inventory", None) or {}).get("bgp") or {}
        if _invalid_origin_available(ctx.net_env):
            rows.append(
                {
                    "host_name": str(bgp["leaker_device"]),
                    "variant": "rpki_invalid_origin",
                }
            )
        return rows

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        if inject.get("variant") == "rpki_invalid_origin":
            bgp = (getattr(ctx.net_env, "inventory", None) or {}).get("bgp") or {}
            if not _invalid_origin_available(ctx.net_env) or inject.get(
                "host_name"
            ) != bgp.get("leaker_device"):
                raise ValueError(
                    "RPKI-invalid origin requires the declared ISP origin router"
                )
            return
        targets = _scenario_targets(ctx.net_env)
        # Rows without a target prefix (published before the scenario declared
        # its hijacks) hijack the default target from an access router.
        if targets and inject.get("target_network"):
            wanted = (inject.get("host_name"), inject.get("target_network"))
            if wanted not in {(t.hijacker, t.prefix) for t in targets}:
                raise ValueError(
                    f"bgp_hijacking {wanted!r} does not divert a victim's traffic "
                    f"on {ctx.scenario} (topo_size={ctx.topo_size!r})"
                )
            return
        require_role_member(
            ctx,
            cls.root_cause_name,
            inject.get("host_name"),
            "access_routers",
            reason="has no attached end host",
        )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def _default_target_network(self) -> str:
        servers = getattr(self.net_env, "servers", None) or {}
        web_servers = servers.get("web", [])
        if web_servers:
            target_host = web_servers[-1]
        elif getattr(self.net_env, "hosts", None):
            target_host = self.net_env.hosts[-1]
        else:
            nodes = self.runtime.list_nodes()
            hosts = sorted(
                name for name in nodes if any(key in name for key in ("client", "pc"))
            )
            if not hosts:
                raise ValueError("Cannot derive hijack target network: no hosts found")
            target_host = hosts[-1]
        iface = (
            "eth1" if any(key in target_host for key in ("client", "pc")) else "eth0"
        )
        target_network = self.runtime.get_host_ip(target_host, iface, with_prefix=True)
        if not target_network:
            target_network = self.runtime.get_host_ip(target_host, with_prefix=True)
        if not target_network:
            raise ValueError(
                f"Cannot derive hijack target network: no IP on {target_host}"
            )
        net = ipaddress.ip_network(target_network, strict=False)
        if net.prefixlen >= 31:
            return str(net)
        if net.prefixlen >= 25:
            return str(net.supernet(new_prefix=24))
        return str(next(net.subnets(new_prefix=25)))

    def affected_prefixes(self, params=None):
        params = params or self._resolved_params
        if params is not None and params.variant == "rpki_invalid_origin":
            return RPKIInvalidOrigin.affected_prefixes(self, params)
        prefix = getattr(params, "target_network", None) or getattr(
            self, "_target_network", None
        )
        return (str(prefix),) if prefix else ()

    def root_cause_resources(self, params: BGPHijackingParams):
        if params.variant == "rpki_invalid_origin":
            return RPKIInvalidOrigin.root_cause_resources(self, params)
        return [node_resource(params.host_name)]

    def _owned_prefixes(self, prefix: str, hijacker: str) -> dict[str, str]:
        """Routers that legitimately originate ``prefix`` or a prefix covering it.

        A more-specific hijack has no exact-match owner; the owner of the
        covering prefix is the legitimate origin of its addresses.
        """
        bgp = (getattr(self.net_env, "inventory", None) or {}).get("bgp") or {}
        target = ipaddress.ip_network(prefix, strict=False)
        owned: dict[str, str] = {}
        origins = list(bgp.get("originated") or [])
        if not origins:
            declared = getattr(self.net_env, "bgp_origin_prefixes", lambda: {})()
            origins.extend(
                {"device": owner, "prefix": network}
                for network, owner in declared.items()
            )
        for row in origins:
            device, origin = str(row.get("device")), str(row.get("prefix") or "")
            if device == hijacker or not origin:
                continue
            try:
                covers = target.subnet_of(ipaddress.ip_network(origin, strict=False))
            except (TypeError, ValueError):
                continue
            if covers and (
                device not in owned
                or ipaddress.ip_network(owned[device]).prefixlen
                < ipaddress.ip_network(origin, strict=False).prefixlen
            ):
                owned[device] = origin
        return dict(sorted(owned.items()))

    def _prefix_owners(self, prefix: str, hijacker: str) -> list[str]:
        """Routers that legitimately originate ``prefix`` or a covering prefix."""
        return list(self._owned_prefixes(prefix, hijacker))

    def _node_asns(self, nodes: list[str]) -> set[str]:
        """ASNs of ``nodes`` from the scenario's BGP inventory."""
        bgp = (getattr(self.net_env, "inventory", None) or {}).get("bgp") or {}
        asns = {
            str(row.get("asn"))
            for row in bgp.get("nodes") or []
            if row.get("device") in nodes and row.get("asn") is not None
        }
        design = getattr(self.net_env, "routing_design", None)
        if design is not None:
            asns.update(
                str(router.asn)
                for name, router in design.routers.items()
                if name in nodes and router.asn is not None
            )
        return asns

    def oracle_playbook(self, params: BGPHijackingParams) -> OraclePlaybook:
        if params.variant == "rpki_invalid_origin":
            return RPKIInvalidOrigin.oracle_playbook(self, params)
        hijacker = params.host_name
        prefix = str(
            ipaddress.ip_network(
                params.target_network or self._default_target_network(), strict=False
            )
        )
        if srl_cli(self.net_env, hijacker):
            return self._srl_playbook(params, prefix)
        owners = self._prefix_owners(prefix, hijacker)
        owner_asns = self._node_asns(owners)
        # Without a known owner, the owner's path on the hijacker shows it.
        owner_seen = (
            []
            if owners
            else [
                Check(
                    f"{hijacker} also holds a path for {prefix} from another origin",
                    _other_origin_seen,
                )
            ]
        )
        steps = []
        if params.observer_host and params.probe_dst_ip:
            steps.append(
                shell(
                    params.observer_host,
                    f"traceroute -n -w 1 -q 1 -m 12 {params.probe_dst_ip} 2>&1 "
                    f"|| ping -c 3 -W 1 {params.probe_dst_ip}",
                    f"Trace where {params.observer_host}'s traffic to "
                    f"{params.probe_dst_ip} ends.",
                    timeout=40,
                )
            )
        steps.append(
            router_cli(
                self.net_env,
                hijacker,
                "Read the originator interface addresses.",
                frr="ip -o -4 addr show",
                iosxr_command="show ipv4 interface brief",
                sonic_command="show ip interfaces",
                routeros_command=(
                    "/ip address print terse; /routing bgp instance print terse"
                ),
                save_as="hijacker_addrs",
            )
        )
        local_origin = all_of(
            contains(
                r"\bsourced\b[^\n]*\bbest\b",
                "the prefix is locally sourced and selected",
            ),
            contains(
                r"Advertised to (?:non peer-group )?peers:\n\s+\S",
                "the prefix is advertised to peers",
            ),
            *owner_seen,
        )
        if native_router_profile(self.net_env, hijacker) == "iosxr":
            local_origin = all_of(
                contains(
                    r"(?m)^\s*Local\s*$|0\.0\.0\.0 from 0\.0\.0\.0",
                    "the BGP path has local origin",
                ),
                contains(r"Advertised to", "the path is advertised"),
                lacks(r"Not advertised to any", "a peer receives the path"),
            )
        elif native_router_profile(self.net_env, hijacker) == "routeros":
            local_origin = contains(
                r"blackhole|gateway=",
                "the originated prefix has a local forwarding entry",
            )
        steps.append(
            router_cli(
                self.net_env,
                hijacker,
                f"Read {prefix} and BGP state on {hijacker}.",
                local_origin,
                frr=vtysh(f"show bgp ipv4 unicast {prefix}", "show bgp summary"),
                iosxr_command=f"show bgp ipv4 unicast {prefix}",
                routeros_command=f"/routing route print detail where dst-address={prefix}",
                evidence=True,
                save_as="hijacker",
            )
        )
        observer = params.symptom_host
        if observer and observer != hijacker:

            def best_from_hijacker(output, outputs):
                if native_router_profile(self.net_env, hijacker) == "routeros":
                    from nika.net_env.utils.routeros.parse import routeros_terse_rows
                    from nika.net_env.vendor_validation import routeros_bgp_table

                    asns = {
                        row["as"]
                        for row in routeros_terse_rows(outputs["hijacker_addrs"])
                        if row.get("as", "").isdigit()
                    } - owner_asns
                    return any(
                        path["best"] and path["aspath"].split()[-1:] == [asn]
                        for path in routeros_bgp_table(output).get(prefix, [])
                        for asn in asns
                    )
                text = outputs.get("hijacker", "")
                ids = re.findall(
                    r"\b(?:\d{1,3}\.){3}\d{1,3}\b", outputs.get("hijacker_addrs", "")
                )
                asn = re.search(r"local AS number (\d+)", text)
                if asn and asn[1] not in owner_asns:
                    ids.append(f"AS{asn[1]}")
                return _best_from_hijacker(f"hijacker={','.join(ids)}\n{output}")

            steps.append(
                router_cli(
                    self.net_env,
                    observer,
                    f"Read {observer}'s best path for {prefix}.",
                    compare(
                        f"{observer} selects a path originated by {hijacker}",
                        best_from_hijacker,
                    ),
                    frr=vtysh(f"show bgp ipv4 unicast {prefix}"),
                    iosxr_command=f"show bgp ipv4 unicast {prefix}",
                    routeros_command=f"/routing route print detail where dst-address={prefix}",
                    evidence=True,
                )
            )
        owned = self._owned_prefixes(prefix, hijacker)
        for owner in owners[:1]:
            steps.append(
                router_cli(
                    self.net_env,
                    owner,
                    f"Check the legitimate origin of {owned[owner]} on {owner}.",
                    contains(
                        r"\bsourced\b|Local|\b0\.0\.0\.0\b",
                        f"{owner} originates {owned[owner]} as well",
                    ),
                    frr=vtysh(f"show bgp ipv4 unicast {owned[owner]}"),
                    iosxr_command=f"show bgp ipv4 unicast {owned[owner]}",
                    routeros_command=f"/routing route print detail where dst-address={owned[owner]}",
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=(
                f"{hijacker} originates {prefix}, a prefix it does not own, into BGP "
                "and attracts traffic meant for the legitimate origin."
            ),
            steps=steps,
        )

    def _srl_advertised_to(self, host: str, prefix: str) -> list[str]:
        data = self.runtime.lab_api.srl_read(
            host,
            "network-instance default bgp-rib afi-safi ipv4-unicast "
            "ipv4-unicast rib-in-out rib-out-post",
        )
        return sorted(
            {
                row["neighbor"]
                for row in _srl_bgp_routes(data)
                if row.get("prefix") == prefix
            }
        )

    def _srl_observer_paths(self, params, prefix):
        if not params.symptom_host:
            return None
        paths = srl_bgp_prefix_paths(
            self.runtime.lab_api.srl_exec_cli(
                params.symptom_host,
                "info from state network-instance default bgp-rib | as json",
            ),
            prefix,
        )
        return sorted(
            (row["aspath"], tuple(row["nexthops"]), row["originator"] or "")
            for row in paths
            if row["best"]
        )

    def _inject_srl(self, params: BGPHijackingParams, prefix: str) -> None:
        host = params.host_name
        api = self.runtime.lab_api
        network = ipaddress.ip_network(prefix, strict=False)
        self._target_network = str(network)
        owned_elsewhere = self._prefix_owners(str(network), host) or any(
            owner != host and network.subnet_of(ipaddress.ip_network(prefix))
            for prefix, owner in self.net_env.bgp_origin_prefixes().items()
        )
        if not owned_elsewhere:
            raise RuntimeCapabilityError(
                "SR Linux hijacking needs an existing business prefix owned by another router"
            )
        self._srl_exports_before = self._srl_advertised_to(host, str(network))
        self._srl_observer_before = self._srl_observer_paths(params, str(network))
        data = api.srl_read(host, "interface lo0", state=False)
        interfaces = data.get("interface", [])
        self._srl_lo0_before = data
        self._srl_had_lo0 = bool(interfaces)
        used = {
            row["index"]
            for interface in interfaces
            for row in interface.get("subinterface", [])
        }
        index = next(i for i in range(1, 4095) if i not in used)
        self._srl_origin = f"lo0.{index}"
        self._srl_origin_index = index
        address = params.probe_dst_ip or str(network.network_address + 1)
        if ipaddress.ip_address(address) not in network:
            raise ValueError(f"Probe address {address} is outside {network}")
        routing_policy = api.srl_read(host, "routing-policy", state=False)
        self._srl_policy_before = routing_policy
        policy = routing_policy["routing-policy"]
        bgp = api.srl_read(host, "network-instance default protocols bgp")[
            "network-instance"
        ][0]["protocols"]["bgp"]
        exports = {
            name
            for neighbor in bgp.get("neighbor", [])
            for family in neighbor.get("afi-safi", [])
            if family["afi-safi-name"].split(":")[-1] == "ipv4-unicast"
            for name in family.get("export-policy", [])
        }
        # Admit the local origin through the existing export gates, including
        # a renderer's prefix allow-list before the business-prefix policy.
        prefix_sets = {
            statement["match"]["prefix-set"]
            for rule in policy.get("policy", [])
            if rule["name"] in exports
            for statement in rule.get("statement", [])
            if statement.get("match", {}).get("prefix-set")
            and not statement["match"].get("protocol")
            and statement.get("action", {}).get("policy-result")
            in {"accept", "next-policy", "next-statement"}
        }
        self._srl_export_additions = [
            row["name"]
            for row in policy.get("prefix-set", [])
            if row["name"] in prefix_sets
            and not any(
                entry["ip-prefix"] == str(network) for entry in row.get("prefix", [])
            )
        ]
        api.srl_configure(
            host,
            *(
                f"/routing-policy prefix-set {name} prefix {network} mask-length-range exact"
                for name in self._srl_export_additions
            ),
            *([] if interfaces else ["/interface lo0 admin-state enable"]),
            f"/interface lo0 subinterface {index} admin-state enable",
            f"/interface lo0 subinterface {index} ipv4 admin-state enable",
            f"/interface lo0 subinterface {index} ipv4 address {address}/{network.prefixlen}",
            f"/network-instance default interface {self._srl_origin}",
        )
        if not wait_until(
            lambda: self._srl_advertised_to(host, str(network)), 60, interval_s=3
        ):
            raise RuntimeError(f"{host} did not advertise hijacked prefix {network}")

    def _srl_playbook(self, params, prefix):
        host = params.host_name
        steps = [
            srl(
                host,
                "info from state network-instance default bgp-rib afi-safi "
                "ipv4-unicast ipv4-unicast rib-in-out rib-out-post "
                f"route {prefix} neighbor * path-id * | as json",
                f"Check that {host} exports {prefix} to BGP peers.",
                Check(
                    "the unauthorized prefix is advertised to a peer",
                    lambda output: any(
                        row.get("prefix") == prefix
                        for row in _srl_bgp_routes(srl_json(output))
                    ),
                ),
                evidence=True,
            ),
        ]
        # Read the route origin as well as export; an exported transit route
        # alone does not show unauthorized local origination.
        steps.append(
            srl(
                host,
                "info from state network-instance default route-table ipv4-unicast "
                f"route {prefix} id * route-type * route-owner * origin-network-instance * | as json",
                f"Check that {host} locally originates {prefix}.",
                Check(
                    "the business prefix is locally connected on the hijacker",
                    lambda output: any(
                        row.get("ipv4-prefix") == prefix
                        and row.get("active")
                        and row.get("route-type", "").split(":")[-1] == "local"
                        for row in _srl_bgp_routes(srl_json(output))
                    ),
                ),
                evidence=True,
            )
        )
        if params.symptom_host:
            observed = {}
            hijacker_asns = self._node_asns([host])
            owner_asns = self._node_asns(self._prefix_owners(prefix, host))

            def selected_route(output):
                return next(
                    (
                        row
                        for row in _srl_bgp_routes(srl_json(output))
                        if row.get("prefix") == prefix and row.get("best-route")
                    ),
                    None,
                )

            def attribute_args(outputs):
                route = selected_route(outputs["observer_paths"])
                if route is None:
                    raise ValueError("No live best BGP path for the selected prefix")
                index = int(route["attr-id"])
                return {
                    "device_name": params.symptom_host,
                    "command": f"info from state network-instance default bgp-rib attr-sets attr-set {index} | as json",
                }

            def selected_origin(output, outputs):
                data = srl_json(outputs["observer_paths"])
                route = selected_route(outputs["observer_paths"])
                if route is None:
                    return False
                rib = data["network-instance"][0]["bgp-rib"]
                rib["afi-safi"][0]["ipv4-unicast"]["local-rib"]["route"] = [route]
                rib["attr-sets"] = srl_json(output)["network-instance"][0]["bgp-rib"][
                    "attr-sets"
                ]
                return any(
                    row["best"]
                    and (
                        row["aspath"].split()[-1:]
                        in [[asn] for asn in hijacker_asns - owner_asns]
                        if hijacker_asns - owner_asns
                        else (
                            set(row["nexthops"]) & observed.get("addresses", set())
                            or row["originator"] in observed.get("addresses", set())
                        )
                    )
                    for row in srl_bgp_prefix_paths(json.dumps(data), prefix)
                )

            def remember(output):
                data = srl_json(output)
                observed["addresses"] = {
                    str(ipaddress.ip_interface(address["ip-prefix"]).ip)
                    for interface in data.get("interface", [])
                    for subinterface in interface.get("subinterface", [])
                    for address in subinterface.get("ipv4", {}).get("address", [])
                    if ipaddress.ip_interface(address["ip-prefix"]).ip
                    not in ipaddress.ip_network(prefix)
                }
                return bool(observed["addresses"])

            steps.extend(
                [
                    srl(
                        host,
                        "info from running interface * subinterface * ipv4 address * | as json",
                        f"Read {host}'s addresses to identify its BGP next hop.",
                        Check(
                            "the hijacker has an address outside the target prefix",
                            remember,
                        ),
                    ),
                    srl(
                        params.symptom_host,
                        "info from state network-instance default bgp-rib afi-safi "
                        "ipv4-unicast ipv4-unicast local-rib "
                        f"route {prefix} neighbor * origin-protocol * path-id * | as json",
                        f"Check that {params.symptom_host} prefers the unauthorized origin.",
                        Check(
                            "the selected prefix has a live best BGP path",
                            lambda output: selected_route(output) is not None,
                        ),
                        evidence=True,
                        attempts=6,
                        save_as="observer_paths",
                    ),
                    replace(
                        srl(
                            params.symptom_host,
                            "",
                            "Read the selected best path's attributes.",
                            compare(
                                "the observer's best BGP path originates at the hijacker",
                                selected_origin,
                            ),
                            evidence=True,
                            attempts=6,
                        ),
                        args=attribute_args,
                    ),
                ]
            )
        owned = self._owned_prefixes(prefix, host)
        for owner in list(owned)[:1]:
            owner_prefix = owned[owner]
            steps.append(
                srl(
                    owner,
                    "info from state network-instance default route-table ipv4-unicast "
                    f"route {owner_prefix} id * route-type * route-owner * origin-network-instance * | as json",
                    f"Read the legitimate covering prefix {owner_prefix} on {owner}.",
                    Check(
                        "the legitimate owner has an active connected prefix covering the hijack",
                        lambda output: any(
                            row.get("ipv4-prefix") == owner_prefix
                            and row.get("active")
                            and row.get("route-type", "").split(":")[-1] == "local"
                            for row in _srl_bgp_routes(srl_json(output))
                        ),
                    ),
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=f"{host} originates and exports {prefix}, which belongs to another router.",
            steps=steps,
        )

    def inject_fault(self, params: BGPHijackingParams):
        if params.variant == "rpki_invalid_origin":
            return RPKIInvalidOrigin.inject_fault(self, params)
        target_network = (
            params.target_network
            if params.target_network is not None
            else self._default_target_network()
        )
        self._target_network = target_network
        if srl_cli(self.net_env, params.host_name):
            self._inject_srl(params, target_network)
            return
        ops = native_fault_ops(self, params.host_name, "originate_bgp_prefix")
        if ops is not None:
            self._native_bgp_ops = ops
            self._native_bgp_config = ops.save_config()
            self._native_bgp_networks = sorted(ops.bgp_networks())
            # The provider also admits the prefix through the router's export
            # policy, the native form of ``export_prefix_list``.
            ops.originate_bgp_prefix(target_network)
            # Routers may hold updates for their minimum advertisement
            # interval (30 s for eBGP on IOS XR); inject returns once sent.
            network = str(ipaddress.ip_network(target_network, strict=False))
            if not wait_until(lambda: ops.bgp_advertised_to(network), 60, interval_s=5):
                raise RuntimeError(
                    f"{type(self).__name__}: {ops.profile} on {params.host_name!r} "
                    f"did not advertise hijacked prefix {network}."
                )
            self.logger.info(
                f"Injected BGP hijacking on {params.host_name}: {target_network} "
                f"({ops.profile})."
            )
            return
        match self.lab_backend:
            case "kathara":
                asn_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
                self._bgp_asn = asn_number
                self._bgp_running = checked_exec(
                    self.runtime, params.host_name, "vtysh -c 'show running-config'"
                )
                paths = checked_exec(
                    self.runtime,
                    params.host_name,
                    'for file in /etc/frr/*.conf; do test ! -f "$file" || printf \'%s\\n\' "$file"; done',
                )
                self._bgp_files = [
                    FileSnapshot.capture(self.runtime, params.host_name, path)
                    for path in sorted(
                        set(paths.splitlines())
                        | {FRR_CONF, f"{FRR_DIR}/bgpd.conf", f"{FRR_DIR}/zebra.conf"}
                    )
                ]
                self._origin_link = checked_exec(
                    self.runtime,
                    params.host_name,
                    f"if ip link show {BGP_ORIGIN_IFACE} >/dev/null 2>&1; then echo present; fi",
                )
                self._origin_link_up = not self._origin_link or "UP" in json.loads(
                    checked_exec(
                        self.runtime,
                        params.host_name,
                        f"ip -j link show dev {BGP_ORIGIN_IFACE}",
                    )
                )[0].get("flags", [])
                self._origin_addresses = (
                    checked_exec(
                        self.runtime,
                        params.host_name,
                        f"ip -4 -o addr show dev {BGP_ORIGIN_IFACE}",
                    )
                    if self._origin_link
                    else ""
                )
                self._origin_local_routes = checked_exec(
                    self.runtime,
                    params.host_name,
                    f"ip -4 route show table local exact {shlex.quote(target_network)}",
                )
                # Off ``lo`` so the IGP does not carry it: only BGP spreads a hijack.
                self.runtime.exec(params.host_name, ENSURE_BGP_ORIGIN_IFACE_CMD)
                self._hijack_permissions = {}
                for router in self.net_env.frr_routers():
                    config = checked_exec(
                        self.runtime, router, "vtysh -c 'show running-config'"
                    )
                    additions = hijack_prefix_permissions(
                        config,
                        target_network,
                        params.export_prefix_list
                        if router == params.host_name
                        else None,
                    )
                    if not additions:
                        continue
                    # Free sequence numbers have no original entry to overwrite.
                    self._hijack_permissions[router] = {
                        f"ip prefix-list {name} seq {seq}": None
                        for name, seq in additions
                    }
                    checked_exec(
                        self.runtime,
                        router,
                        frr_config_command(
                            *(
                                f"ip prefix-list {name} seq {seq} permit {target_network}"
                                for name, seq in additions
                            )
                        ),
                    )
                    if router != params.host_name:
                        frr_save_config(self.runtime, router)
                    checked_exec(
                        self.runtime,
                        router,
                        "vtysh -c 'clear ip bgp * soft in' -c 'clear ip bgp * soft out'",
                    )
                self.runtime.exec(
                    params.host_name,
                    frr_config_command(
                        f"interface {BGP_ORIGIN_IFACE}",
                        f"ip address {target_network}",
                        "exit",
                        f"router bgp {asn_number}",
                        f"network {target_network}",
                    ),
                )
                frr_save_config(self.runtime, params.host_name)
                # The local-table route keeps answering every address in the
                # prefix, as an address with that prefix on ``lo`` did.
                self.runtime.exec(
                    params.host_name,
                    f"ip route replace local {target_network} "
                    f"dev {BGP_ORIGIN_IFACE} table local",
                )
                if not wait_until(
                    lambda: self.verify_fault(params)["verified"], 60, interval_s=2
                ):
                    raise RuntimeError(
                        f"Hijacked prefix {target_network} did not propagate"
                    )
                self.logger.info(
                    f"Injected BGP hijacking on {params.host_name}: {target_network}."
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def recover_fault(self, params: BGPHijackingParams) -> dict:
        if params.variant == "rpki_invalid_origin":
            return RPKIInvalidOrigin.recover_fault(self, params)
        host = params.host_name
        target = getattr(self, "_target_network", None)
        if target is None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=False,
                details={"error": "Pre-injection BGP state is unavailable."},
            )
        if srl_cli(self.net_env, host):
            api = self.runtime.lab_api
            configuration_restored = (
                api.srl_read(host, "interface lo0", state=False) == self._srl_lo0_before
                and api.srl_read(host, "routing-policy", state=False)
                == self._srl_policy_before
            )
            if not configuration_restored:
                api.srl_configure(
                    host,
                    *(
                        f"delete /routing-policy prefix-set {name} prefix {target} mask-length-range exact"
                        for name in self._srl_export_additions
                    ),
                    f"delete /network-instance default interface {self._srl_origin}",
                    (
                        f"delete /interface lo0 subinterface {self._srl_origin_index}"
                        if self._srl_had_lo0
                        else "delete /interface lo0"
                    ),
                )
            restored = wait_until(
                lambda: (
                    self._srl_advertised_to(host, target) == self._srl_exports_before
                    and self._srl_observer_paths(params, target)
                    == self._srl_observer_before
                ),
                60,
                interval_s=3,
            )
            return build_verify_result(
                self.root_cause_name,
                restored
                and api.srl_read(host, "interface lo0", state=False)
                == self._srl_lo0_before
                and api.srl_read(host, "routing-policy", state=False)
                == self._srl_policy_before,
                {"host": host, "target_network": target},
            )
        ops = getattr(self, "_native_bgp_ops", None) or native_fault_ops(
            self, host, "originate_bgp_prefix"
        )
        if ops is not None:
            saved = getattr(self, "_native_bgp_config", None)
            if saved is None:
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=False,
                    details={
                        "error": "Pre-injection native router configuration is unavailable."
                    },
                )
            ops.restore_config(saved)
            verified = sorted(ops.bgp_networks()) == self._native_bgp_networks
        elif self.lab_backend == "kathara":
            if not hasattr(self, "_bgp_running"):
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=False,
                    details={
                        "error": "Pre-injection FRR configuration is unavailable."
                    },
                )
            running = self._bgp_running
            current_running = checked_exec(
                self.runtime, host, "vtysh -c 'show running-config'"
            )
            commands = []
            network_line = rf"^[ \t]+network {re.escape(target)}$"
            if not re.search(network_line, running, re.M) and re.search(
                network_line, current_running, re.M
            ):
                commands.extend(
                    [f"router bgp {self._bgp_asn}", f"no network {target}", "exit"]
                )
            if not self._origin_link:
                if re.search(rf"^interface {BGP_ORIGIN_IFACE}$", current_running, re.M):
                    commands.append(f"no interface {BGP_ORIGIN_IFACE}")
            else:
                block = re.search(
                    rf"^interface {BGP_ORIGIN_IFACE}\n(.*?)(?=^!)", running, re.M | re.S
                )
                current_block = re.search(
                    rf"^interface {BGP_ORIGIN_IFACE}\n(.*?)(?=^!)",
                    current_running,
                    re.M | re.S,
                )
                if (
                    (not block or f" ip address {target}" not in block[1].splitlines())
                    and current_block
                    and f" ip address {target}" in current_block[1].splitlines()
                ):
                    commands.extend(
                        [
                            f"interface {BGP_ORIGIN_IFACE}",
                            f"no ip address {target}",
                            "exit",
                        ]
                    )
            if not self._origin_link:
                # FRR rejects deleting an interface that still exists in the kernel.
                checked_exec(
                    self.runtime,
                    host,
                    f"if ip link show {BGP_ORIGIN_IFACE} >/dev/null 2>&1; then ip link del {BGP_ORIGIN_IFACE}; fi",
                )
            if commands:
                checked_exec(self.runtime, host, frr_config_command(*commands))
            if self._origin_link and not self._origin_link_up:
                checked_exec(
                    self.runtime, host, f"ip link set dev {BGP_ORIGIN_IFACE} down"
                )
            route = checked_exec(
                self.runtime,
                host,
                f"ip -4 route show table local exact {shlex.quote(target)}",
            )
            if route and not self._origin_local_routes:
                checked_exec(
                    self.runtime,
                    host,
                    f"ip route del local {shlex.quote(target)} table local",
                )
            for original in self._origin_local_routes.splitlines():
                checked_exec(
                    self.runtime, host, "ip route replace " + original + " table local"
                )
            permissions_restored = True
            for router, entries in self._hijack_permissions.items():
                checked_exec(
                    self.runtime,
                    router,
                    frr_config_command(*(f"no {entry}" for entry in entries)),
                )
                checked_exec(
                    self.runtime,
                    router,
                    "vtysh -c 'clear ip bgp * soft in' -c 'clear ip bgp * soft out'",
                )
                if router != host:
                    frr_save_config(self.runtime, router)
                current_config = checked_exec(
                    self.runtime, router, "vtysh -c 'show running-config'"
                )
                permissions_restored &= all(
                    not re.search(rf"^{re.escape(entry)} .*", current_config, re.M)
                    for entry in entries
                )
            for snapshot in self._bgp_files:
                snapshot.restore(self.runtime, host)
            current = checked_exec(self.runtime, host, "vtysh -c 'show running-config'")
            link = checked_exec(
                self.runtime,
                host,
                f"if ip link show {BGP_ORIGIN_IFACE} >/dev/null 2>&1; then ip -j link show dev {BGP_ORIGIN_IFACE}; fi",
            )
            addresses = (
                checked_exec(
                    self.runtime, host, f"ip -4 -o addr show dev {BGP_ORIGIN_IFACE}"
                )
                if link
                else ""
            )
            local_routes = checked_exec(
                self.runtime,
                host,
                f"ip -4 route show table local exact {shlex.quote(target)}",
            )
            # Routine operator changes can alter descriptions and unrelated
            # policies during diagnosis. Check every field the injector owns.
            verified = (
                permissions_restored
                and _hijack_config_state(current, target, params.export_prefix_list)
                == _hijack_config_state(running, target, params.export_prefix_list)
                and bool(link) == bool(self._origin_link)
                and (
                    not link
                    or ("UP" in json.loads(link)[0].get("flags", []))
                    == self._origin_link_up
                )
                and re.findall(r"\binet (\S+)", addresses)
                == re.findall(r"\binet (\S+)", self._origin_addresses)
                and local_routes == self._origin_local_routes
            )
        else:
            raise RuntimeCapabilityError(
                f"BGP recovery is unsupported on {self.lab_backend!r}."
            )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": host, "target_network": target},
        )

    def verify_fault(self, params: BGPHijackingParams) -> dict:
        """Verify the router is advertising the hijacked network via BGP."""
        if params.variant == "rpki_invalid_origin":
            return RPKIInvalidOrigin.verify_fault(self, params)
        target_network = (
            params.target_network
            if params.target_network is not None
            else getattr(self, "_target_network", None)
            or self._default_target_network()
        )
        if srl_cli(self.net_env, params.host_name):
            target_network = str(ipaddress.ip_network(target_network, strict=False))
            advertised = self._srl_advertised_to(params.host_name, target_network)
            return build_verify_result(
                self.root_cause_name,
                bool(advertised)
                and target_network
                in self.runtime.lab_api.srl_routes(params.host_name, "local"),
                {"host": params.host_name, "advertised_to": advertised},
            )
        ops = native_fault_ops(self, params.host_name, "bgp_advertised_to")
        if ops is not None:
            network = str(ipaddress.ip_network(target_network, strict=False))
            advertised_to = ops.bgp_advertised_to(network)
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(advertised_to),
                details={
                    "host": params.host_name,
                    "target_network": network,
                    "advertised_to": advertised_to,
                },
            )
        match self.lab_backend:
            case "kathara":
                # The hijacker's BGP table must source the prefix as its best
                # path and send it to a peer; whether peers accept it is the symptom.
                table = self.runtime.exec(
                    params.host_name,
                    f"vtysh -c 'show bgp ipv4 unicast {target_network} json' 2>/dev/null",
                )
                try:
                    entry = json.loads(table) or {}
                except ValueError:
                    entry = {}
                originated = any(
                    path.get("sourced") and (path.get("bestpath") or {}).get("overall")
                    for path in entry.get("paths") or []
                )
                network_prefix = target_network.split("/")[0]
                origin_output = self.runtime.exec(
                    params.host_name, f"ip addr show {BGP_ORIGIN_IFACE} 2>/dev/null"
                ).strip()
                has_origin_ip = network_prefix in origin_output
                advertised_to = sorted(entry.get("advertisedTo") or {})
                propagated = True
                if params.symptom_host and params.symptom_host != params.host_name:
                    identifiers = {
                        f"AS{self.runtime.frr_get_bgp_asn_number(params.host_name)}"
                    }
                    identifiers.update(
                        re.findall(
                            r"inet ([^/\s]+)",
                            self.runtime.exec(params.host_name, "ip -o -4 addr show"),
                        )
                    )
                    observed = self.runtime.exec(
                        params.symptom_host,
                        vtysh(f"show bgp ipv4 unicast {target_network}"),
                    )
                    propagated = _best_from_hijacker(
                        f"hijacker={','.join(identifiers)}\n{observed}"
                    )
                verified = (
                    originated and has_origin_ip and bool(advertised_to) and propagated
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "target_network": target_network,
                        "originated_best": originated,
                        "propagated": propagated,
                        "advertised_to": advertised_to,
                        "has_origin_ip": has_origin_ip,
                    },
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )
