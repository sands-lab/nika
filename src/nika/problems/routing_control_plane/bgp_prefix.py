"""BGP aggregate import filters with an incorrect prefix-length match."""

from __future__ import annotations

import ipaddress
import json
import re

from pydantic import BaseModel, Field

from nika.net_env.native_routers import router_sketches
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import attached_end_hosts, choice
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    router_cli,
    vtysh,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.runtime.base import RuntimeCapabilityError


def import_prefix_ranges(config: str) -> dict[str, tuple[str, int]]:
    """Single-entry permit lists referenced by a bound inbound route map.

    A list of exact /24 entries has no broader length range to lose. Lists
    with multiple entries or ge constraints need a different transformation.
    """
    imports = set(re.findall(r"^\s+neighbor \S+ route-map (\S+) in$", config, re.M))
    names = {
        name
        for policy, body in re.findall(
            r"^route-map (\S+) permit \d+\n(.*?)(?=^route-map |^!|\Z)",
            config,
            re.M | re.S,
        )
        if policy in imports
        for name in re.findall(
            r"^[ \t]+match ip address prefix-list (\S+)$", body, re.M
        )
    }
    ranges = {}
    for name in sorted(names):
        entries = re.findall(rf"^ip prefix-list {re.escape(name)} (.*)$", config, re.M)
        if len(entries) != 1:
            continue
        match = re.fullmatch(
            r"seq \d+ permit (\d+\.\d+\.\d+\.\d+/\d+)(?: le (\d+))?", entries[0]
        )
        if match:
            network = ipaddress.ip_network(match[1])
            ranges[name] = (str(network), int(match[2] or network.prefixlen))
    return ranges


def _frr_state(output: str) -> tuple[set[str], set[str]]:
    """Read consecutive summary/table JSON documents from one vtysh call."""
    decoder = json.JSONDecoder()
    summary, consumed = decoder.raw_decode(output.lstrip())
    table = json.loads(output.lstrip()[consumed:].strip())
    peers = {
        peer
        for peer, state in summary["ipv4Unicast"]["peers"].items()
        if state.get("state") == "Established"
    }
    routes = {
        prefix
        for prefix, paths in table["routes"].items()
        if any(path.get("path", "").strip() for path in paths)
    }
    return peers, routes


class BGPPrefixListLengthMismatchParams(BaseModel):
    host_name: str = Field(
        description="Router carrying a length-bounded import filter."
    )


class BGPPrefixListLengthMismatch(ProblemBase):
    """An aggregate import filter matches only the aggregate's own length.

    The exact le omission follows vendor semantics and similar filter incidents,
    rather than a single postmortem. The BitFolk 2016 incident report describes
    loss of de-aggregates after a transit filter rebuild:
    https://mailman.bitfolk.com/lurker/message/20160501.204902.6ff9c7e5.en.html
    Cisco's vendor troubleshooting document explains aggregate-only filtering:
    https://www.cisco.com/c/en/us/support/docs/ip/border-gateway-protocol-bgp/13750-22.html
    FRR's vendor command reference defines an omitted ge/le as exact length:
    https://docs.frrouting.org/en/latest/filter.html#ip-prefix-list
    RouterOS's vendor reference distinguishes dst equality from containment:
    https://help.mikrotik.com/docs/spaces/ROS/pages/74678285/Route+Selection+and+Filters
    Mahajan et al.'s SIGCOMM 2002 measurement study covers BGP misconfiguration:
    https://www.caida.org/archive/networkingbib/entries/mahajan02understanding/

    RouterOS needs dst == aggregate: deleting dst-len from an in expression
    would still accept more-specific routes. Policy bindings and actions stay
    intact. Ground truth identifies the router whose filter changed.
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "bgp_prefix_list_length_mismatch"
    description = "A BGP import prefix-list matches the aggregate without its required length range."
    symptom_probe = "custom"
    symptom_class = "control_plane"
    effect_protocol = "bgp"
    TAGS = ["bgp"]
    COMPATIBLE_COLUMNS = frozenset({"enterprise_branch"})
    supported_backends = ("kathara",)
    native_router_operations = frozenset(
        {
            "bgp_import_prefix_ranges",
            "set_bgp_import_prefix_range",
            "bgp_established_peers",
            "remote_bgp_prefixes",
        }
    )
    Params = BGPPrefixListLengthMismatchParams

    @classmethod
    def _targets(cls, ctx):
        return [
            host
            for host, (config, _startup) in router_sketches(ctx.net_env).items()
            if host in ctx.net_env.routers
            and attached_end_hosts(ctx.net_env, host)
            and any(
                ipaddress.ip_network(prefix).prefixlen < length
                for prefix, length in import_prefix_ranges(config).values()
            )
        ]

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = cls._targets(ctx)
        if not targets:
            raise ValueError(
                "No length-bounded BGP import filter in the declared design"
            )
        return {"host_name": choice(ctx.rng, targets, targets[0])}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        if inject.get("host_name") not in cls._targets(ctx):
            raise ValueError(
                "Target needs a length-bounded BGP import filter and an end host"
            )

    def root_cause_resources(self, params: BGPPrefixListLengthMismatchParams):
        return [node_resource(params.host_name)]

    def _filters(self, host):
        ops = native_fault_ops(self, host, "bgp_import_prefix_ranges")
        if ops:
            return ops.bgp_import_prefix_ranges()
        return import_prefix_ranges(
            self.runtime.exec(host, vtysh("show running-config"))
        )

    def _state(self, host):
        ops = native_fault_ops(self, host, "bgp_established_peers")
        if ops:
            routes = native_fault_ops(self, host, "remote_bgp_prefixes")
            return ops.bgp_established_peers(), set(routes.remote_bgp_prefixes())
        return _frr_state(
            self.runtime.exec(
                host, vtysh("show bgp summary json", "show bgp ipv4 unicast json")
            )
        )

    def _set_range(self, host, name, prefix, length):
        ops = native_fault_ops(self, host, "set_bgp_import_prefix_range")
        if ops:
            ops.set_bgp_import_prefix_range(name, prefix, length)
            return
        config = self.runtime.exec(host, vtysh("show running-config"))
        entry = re.search(
            rf"^ip prefix-list {re.escape(name)} seq (\d+) permit {re.escape(prefix)}(?: le \d+)?$",
            config,
            re.M,
        )
        if not entry:
            raise RuntimeCapabilityError(f"Import filter {name!r} is absent on {host}")
        suffix = (
            f" le {length}" if length > ipaddress.ip_network(prefix).prefixlen else ""
        )
        result = self.runtime.exec(
            host,
            frr_config_command(
                f"ip prefix-list {name} seq {entry[1]} permit {prefix}{suffix}"
            ),
        )
        if "%" in result:
            raise RuntimeError(f"FRR rejected the prefix-list update: {result}")
        frr_save_config(self.runtime, host)
        self.runtime.exec(host, vtysh("clear bgp * soft in"))

    def inject_fault(self, params: BGPPrefixListLengthMismatchParams):
        host = params.host_name
        self._healthy_filters = {
            k: (p, n)
            for k, (p, n) in self._filters(host).items()
            if ipaddress.ip_network(p).prefixlen < n
        }
        self._healthy_peers, self._healthy_routes = self._state(host)
        if (
            not self._healthy_filters
            or not self._healthy_peers
            or not self._healthy_routes
        ):
            raise RuntimeCapabilityError(
                "A healthy BGP session, learned routes and aggregate import range are required"
            )
        for name, (prefix, _length) in self._healthy_filters.items():
            self._set_range(host, name, prefix, ipaddress.ip_network(prefix).prefixlen)

    def verify_fault(self, params: BGPPrefixListLengthMismatchParams) -> dict:
        expected = getattr(self, "_healthy_filters", {})
        current = self._filters(params.host_name)
        verified = bool(expected) and all(
            current.get(name) == (prefix, ipaddress.ip_network(prefix).prefixlen)
            for name, (prefix, _length) in expected.items()
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "import_filters": current},
        )

    def recover_fault(self, params: BGPPrefixListLengthMismatchParams) -> dict:
        filters = getattr(self, "_healthy_filters", {})
        if not filters:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        for name, (prefix, length) in filters.items():
            self._set_range(params.host_name, name, prefix, length)

        def healthy():
            peers, routes = self._state(params.host_name)
            return self._healthy_peers <= peers and self._healthy_routes <= routes

        restored = wait_until(healthy, 180, interval_s=3)
        peers, routes = self._state(params.host_name)
        return {
            "verified": restored
            and all(
                self._filters(params.host_name).get(k) == v for k, v in filters.items()
            ),
            "details": {"peers": sorted(peers), "routes": sorted(routes)},
        }

    def oracle_playbook(
        self, params: BGPPrefixListLengthMismatchParams
    ) -> OraclePlaybook:
        host = params.host_name
        profile = native_router_profile(self.net_env, host)
        if profile == "routeros":
            from nika.net_env.utils.routeros.parse import (
                routeros_filter_rules,
                routeros_terse_rows,
            )

            def evidence(output):
                sections = output.split("=== CONNECTIONS ===")
                if len(sections) != 2:
                    return False
                sessions = routeros_terse_rows(sections[0])
                connections, _, filters = sections[1].partition("=== FILTERS ===")
                if "=== ROUTES ===" not in filters:
                    return False
                rules, _, routes = filters.partition("=== ROUTES ===")
                peers = {
                    r["remote.address"].split("@", 1)[0]
                    for r in sessions
                    if r.get("remote.address")
                }
                connections = routeros_terse_rows(connections)
                imports = {
                    r["input.filter"]
                    for r in connections
                    if r.get("input.filter") and r.get("remote.address") in peers
                }
                rows = routeros_filter_rules(rules)

                def exact_aggregate(row):
                    match = re.search(
                        r"if \(dst == (\d+\.\d+\.\d+\.\d+/\d+)\) .*accept;",
                        row.get("rule", ""),
                    )
                    return bool(match) and ipaddress.ip_network(match[1]).prefixlen < 24

                return (
                    bool(peers and imports)
                    and all(
                        any(
                            r.get("chain") == chain and exact_aggregate(r) for r in rows
                        )
                        for chain in imports
                    )
                    and not any(
                        r.get("belongs-to", "").startswith("bgp-IP-")
                        for r in routeros_terse_rows(routes)
                    )
                )
        else:

            def evidence(output):
                ranges = import_prefix_ranges(output)
                return (
                    bool(ranges)
                    and any(
                        ipaddress.ip_network(p).prefixlen == n < 24
                        for p, n in ranges.values()
                    )
                    and bool(
                        re.search(
                            r"^\S+\s+4\s+\d+(?:\s+\d+){5}\s+\S+\s+0\s", output, re.M
                        )
                    )
                )

        playbook = OraclePlaybook(
            finding=f"{host}'s bound BGP import filters match only the aggregate length and reject business /24s.",
            steps=[
                router_cli(
                    self.net_env,
                    host,
                    "Compare BGP sessions, imported routes, and the bound import filter's prefix lengths.",
                    Check(
                        "Established sessions have aggregate-only import filters rather than missing policy bindings",
                        evidence,
                    ),
                    frr=vtysh(
                        "show bgp ipv4 unicast summary", "show ip route vrf vrf_corp"
                    )
                    + "; "
                    + vtysh("show running-config")
                    + " | grep -E '^ip prefix-list |^route-map |^ match |^router bgp |^ +neighbor .*route-map'",
                    routeros_command=(
                        '/routing bgp session print terse where established=yes; :put "=== CONNECTIONS ==="; '
                        '/routing bgp connection print terse; :put "=== FILTERS ==="; '
                        '/routing filter rule print terse; :put "=== ROUTES ==="; '
                        "/routing route print terse where bgp and active and afi=ip"
                    ),
                    evidence=True,
                    attempts=12,
                    interval_sec=5,
                )
            ],
        )
        return playbook
