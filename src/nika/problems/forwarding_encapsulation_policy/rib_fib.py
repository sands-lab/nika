"""A stale next hop in the Linux main FIB despite a correct FRR best path."""

import ipaddress
import json
import shlex

from pydantic import BaseModel, Field

from nika.net_env.net_env_pool import coverage_columns
from nika.net_env.verify import (
    frr_bgp_established_peers,
    frr_ospf_full_router_ids,
    ping_stats,
)
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.routing_control_plane.ospf import hosts_behind_router, linked_routers
from nika.problems.support.benchmark_targets import require_node_capability
from nika.problems.support.oracle import Check, OraclePlaybook, OracleStep, shell, vtysh
from nika.problems.support.polling import wait_until
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.spec import NodeRole

_DYNAMIC = {"bgp", "ospf", "isis"}


def rib_routes(raw: str) -> dict[str, dict]:
    """Selected dynamic routes with resolved active IPv4 next hops."""

    def hops(entries):
        result = set()
        for hop in entries:
            if hop.get("resolvedVia"):
                result.update(hops(hop["resolvedVia"]))
            elif hop.get("active") and not hop.get("recursive"):
                if hop.get("ip") and hop.get("interfaceName"):
                    result.add((hop["ip"], hop["interfaceName"]))
        return result

    routes = {}
    for prefix, entries in json.loads(raw).items():
        for row in entries:
            if row.get("selected") and row.get("protocol") in _DYNAMIC:
                resolved = hops(row.get("nexthops") or [])
                if resolved and row.get("table", 254) == 254:
                    routes[prefix] = {
                        "protocol": row["protocol"],
                        "hops": [list(hop) for hop in sorted(resolved)],
                    }
    return routes


def kernel_route(rows: list[dict], prefix: str) -> dict | None:
    """The lowest-metric exact prefix in the Linux main table."""
    matches = [
        row
        for row in rows
        if row.get("dst") == prefix and row.get("table", "main") in ("main", 254)
    ]
    return min(matches, key=lambda row: row.get("metric", 0)) if matches else None


def kernel_hops(row: dict | None) -> list[list[str]]:
    if not row or row.get("type", "unicast") != "unicast":
        return []
    return [
        list(hop)
        for hop in sorted(
            {
                (hop["gateway"], hop["dev"])
                for hop in row.get("nexthops") or [row]
                if hop.get("gateway")
                and hop.get("dev")
                and not set(hop.get("flags") or []) & {"dead", "linkdown"}
            }
        )
    ]


def divergent_routes(rib: dict, rows: list[dict]) -> dict[str, dict]:
    """Compare resolved RIB paths with matching-protocol main-table entries.

    A competing kernel/static route is a different fault. Unresolved kernel
    nexthop IDs are unreadable observations, rather than evidence of loss.
    """
    result = {}
    for prefix, route in rib.items():
        fib = kernel_route(rows, prefix)
        if fib and (
            fib.get("protocol") != route["protocol"]
            or (fib.get("nhid") and not fib.get("nexthops") and not fib.get("dev"))
        ):
            continue
        actual = kernel_hops(fib)
        if actual != route["hops"]:
            result[prefix] = {"rib": route["hops"], "fib": actual}
    return result


class RibFibParams(BaseModel):
    host_name: str = Field(description="Target FRR router with an attached endpoint.")


class RibFibInconsistency(ProblemBase):
    """One main-table FIB entry points outside the selected FRR next-hop set.

    A single next-hop replacement retains the prefix, protocol, and kernel
    metric. FRR 10.5.3 ignores RTM_NEWROUTE notifications tagged with its own
    protocol, leaving the correct RIB and protocol sessions intact. Deletion
    instead triggers zebra's route reinstallation. This models a stale
    forwarding-programming result, with no recurring writer or policy route.
    Implementation reference:
    https://github.com/FRRouting/frr/blob/frr-10.5.3/zebra/rt_netlink.c#L1183

    Research: VeriTable checks forwarding equivalence across tables:
    https://arxiv.org/pdf/1804.07374
    Research: VeriDP (CoNEXT 2016) compares control-plane intent and actual
    forwarding, including external rule modifications:
    https://doi.org/10.1145/2999572.2999605
    Vendor troubleshooting document: Juniper describes RIB/PFE installation
    discrepancies and their traffic loss:
    https://www.juniper.net/documentation/us/en/software/junos/static-routing/topics/concept/pfe-install-error-checking-configure.html
    Related incident reports: pfSense #16120 describes a stale zebra RIB
    preventing BGP installation, and VyOS T5424 describes lost kernel routes
    and nexthop-group installation failures. These motivate the failure class;
    the injection reproduces the divergence rather than either exact bug.
    https://redmine.pfsense.org/issues/16120
    https://vyos.dev/T5424 (read through https://staging.vyos.dev/T5424)
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "rib_fib_inconsistency"
    description = "The forwarding table disagrees with the selected routing-table path."
    symptom_desc = "Some destinations are unreachable while routing sessions remain up."
    symptom_probe = "custom"
    symptom_class = "unreachable"
    TAGS = ["frr"]
    supported_backends = ("kathara",)
    COMPATIBLE_COLUMNS = frozenset(
        column
        for column in coverage_columns()
        if column in {"dc_clos", "campus_lan"} or column.startswith("isp_")
    )
    Params = RibFibParams

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        if device_profile not in (None, "", "frr"):
            return False
        return super().is_compatible(target, device_profile, backend=backend)

    @staticmethod
    def _targets(net_env):
        intent = net_env.probe_intent()
        isolated = intent.isolation_matcher() if intent else lambda _a, _b: False
        endpoints = {
            name
            for name, identity in net_env.machine_identities.items()
            if identity.role in {NodeRole.HOST, NodeRole.SERVICE}
        }
        targets = []
        for router in net_env.routers:
            if "frr" not in net_env.machine_identities[router].capabilities:
                continue
            local = hosts_behind_router(net_env, router)
            if any(
                not isolated(src, dst)
                for src in local
                for dst in endpoints - set(local)
            ):
                targets.append(router)
        return targets

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = cls._targets(ctx.net_env)
        if not targets:
            raise ValueError(
                "RIB/FIB divergence needs an FRR router with a permitted remote endpoint"
            )
        return {"host_name": ctx.rng.choice(targets)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        host = inject.get("host_name")
        require_node_capability(ctx, cls.root_cause_name, host, "frr")
        if host not in cls._targets(ctx.net_env):
            raise ValueError(
                "RIB/FIB divergence needs a permitted remote endpoint path"
            )

    def root_cause_resources(self, params: RibFibParams):
        return [node_resource(params.host_name)]

    def tables(self, host: str) -> tuple[dict, list[dict]]:
        raw = self.runtime.exec(
            host,
            vtysh("show ip route json") + "; echo ===; ip -j -4 route show table main",
        )
        rib, fib = raw.split("===", 1)
        return rib_routes(rib), json.loads(fib)

    def sessions(self, host: str) -> dict[str, list[str]]:
        caps = self.net_env.machine_identities[host].capabilities
        result = {}
        if "bgp" in caps:
            result["bgp"] = sorted(
                frr_bgp_established_peers(
                    self.runtime.exec(host, vtysh("show bgp summary json"))
                )
            )
        if "ospf" in caps:
            result["ospf"] = sorted(
                frr_ospf_full_router_ids(
                    self.runtime.exec(host, vtysh("show ip ospf neighbor"))
                )
            )
        if "isis" in caps:
            from nika.validation.symptom.routing_probes import _isis_neighbors

            result["isis"] = sorted(
                f"{row['system']}|{row['intf']}"
                for row in _isis_neighbors(self.runtime, host) or []
                if str(row["state"]).lower() == "up"
            )
        return result

    def prepare(self, params: RibFibParams) -> dict:
        """Choose a reachable remote endpoint and a directly connected wrong hop."""
        if getattr(self, "_rib_fib", None):
            return self._rib_fib
        host = params.host_name
        if host not in self._targets(self.net_env):
            raise RuntimeCapabilityError(
                "Target must be an FRR router with a permitted remote endpoint path"
            )
        rib, rows = self.tables(host)
        local = hosts_behind_router(self.net_env, host)
        endpoints = [
            name
            for name, identity in sorted(self.net_env.machine_identities.items())
            if identity.role in {NodeRole.HOST, NodeRole.SERVICE} and name not in local
        ]
        sessions = self.sessions(host)
        if not any(sessions.values()):
            raise RuntimeCapabilityError("No healthy routing adjacency on the target")
        for src in local:
            source_ip = self.runtime.get_data_plane_host_ip(src)
            if not source_ip:
                continue
            local_route = json.loads(
                self.runtime.exec(host, f"ip -j -4 route get {source_ip}")
            )[0]
            if local_route.get("gateway") or not local_route.get("dev"):
                continue
            for dst in endpoints:
                address = self.runtime.get_data_plane_host_ip(dst)
                if not address:
                    continue
                prefixes = [
                    p
                    for p in rib
                    if ipaddress.ip_address(address) in ipaddress.ip_network(p)
                ]
                if not prefixes:
                    continue
                prefix = max(prefixes, key=lambda p: ipaddress.ip_network(p).prefixlen)
                route = rib[prefix]
                fib = kernel_route(rows, prefix)
                if (
                    not fib
                    or fib.get("protocol") != route["protocol"]
                    or kernel_hops(fib) != route["hops"]
                ):
                    continue
                if (
                    ping_stats(
                        self.runtime, src, address, count=3, interval_sec=0.2
                    ).loss_percent
                    != 0
                ):
                    continue
                self._rib_fib = {
                    "host": host,
                    "src": src,
                    "dst": address,
                    "prefix": prefix,
                    "wrong_hop": source_ip,
                    "interface": local_route["dev"],
                    "route": route,
                    "metric": fib.get("metric", 0),
                    "sessions": sessions,
                }
                return self._rib_fib
        raise RuntimeCapabilityError(
            "No healthy endpoint path with an equivalent dynamic main-table FIB entry"
        )

    def inject_fault(self, params: RibFibParams):
        state = self.prepare(params)
        command = shlex.join(
            [
                "ip",
                "-4",
                "route",
                "replace",
                state["prefix"],
                "via",
                state["wrong_hop"],
                "dev",
                state["interface"],
                "proto",
                state["route"]["protocol"],
                "metric",
                str(state["metric"]),
            ]
        )
        self.runtime.exec(params.host_name, command)

    def snapshot(self, params: RibFibParams) -> dict:
        state = self._rib_fib
        rib, rows = self.tables(params.host_name)
        return {
            "rib": rib.get(state["prefix"]),
            "fib": kernel_hops(kernel_route(rows, state["prefix"])),
            "sessions": self.sessions(params.host_name),
            "loss_percent": ping_stats(
                self.runtime, state["src"], state["dst"], count=5, interval_sec=0.2
            ).loss_percent,
        }

    def verify_fault(self, params: RibFibParams) -> dict:
        state = self._rib_fib
        rib, rows = self.tables(params.host_name)
        fib = kernel_route(rows, state["prefix"])
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=(
                rib.get(state["prefix"]) == state["route"]
                and fib is not None
                and fib.get("protocol") == state["route"]["protocol"]
                and fib.get("metric", 0) == state["metric"]
                and kernel_hops(fib) == [[state["wrong_hop"], state["interface"]]]
            ),
            details={
                "host": params.host_name,
                "prefix": state["prefix"],
                "rib": rib.get(state["prefix"]),
                "fib": fib,
            },
        )

    def recover_fault(self, params: RibFibParams) -> dict:
        state = getattr(self, "_rib_fib", None)
        if not state or state["host"] != params.host_name:
            return {
                "verified": False,
                "details": {"error": "no_healthy_rib_fib_baseline"},
            }
        # Zebra reinstalls a deleted self-originated route from its unchanged RIB.
        self.runtime.exec(
            params.host_name,
            shlex.join(
                [
                    "ip",
                    "-4",
                    "route",
                    "del",
                    state["prefix"],
                    "proto",
                    state["route"]["protocol"],
                    "metric",
                    str(state["metric"]),
                ]
            ),
        )
        latest = {}

        def healthy():
            latest.update(self.snapshot(params))
            return (
                latest["rib"] == state["route"]
                and latest["fib"] == state["route"]["hops"]
                and latest["sessions"] == state["sessions"]
                and latest["loss_percent"] == 0
            )

        ok = wait_until(healthy, 90, interval_s=3)
        return {"verified": ok, "details": {"prefix": state["prefix"], **latest}}

    def oracle_playbook(self, params: RibFibParams) -> OraclePlaybook:
        transit = sorted(
            {intf for intf, *_ in linked_routers(self.net_env, params.host_name)}
        )
        if not transit:
            raise RuntimeCapabilityError("No router-facing link in the target topology")
        # Reduce output inside the router before the MCP output limit applies.
        script = (
            "import json,sys; rows=json.load(sys.stdin); "
            f"transit={transit!r}; "
            "print(json.dumps([r for r in rows "
            "if r.get('protocol') in ('bgp','ospf','isis') "
            "and any(h.get('gateway') and h.get('dev') not in transit "
            "for h in r.get('nexthops') or [r])]))"
        )
        observed = {}

        def rib_args(outputs):
            rows = json.loads(outputs["fib"])
            row = sorted(rows, key=lambda item: item["dst"])[0]
            prefix = str(ipaddress.ip_network(row["dst"]))
            observed["fib"] = [row]
            return {
                "host_name": params.host_name,
                "command": vtysh(f"show ip route {prefix} json"),
                "timeout": 15,
            }

        return OraclePlaybook(
            finding=f"{params.host_name}'s main FIB disagrees with the selected FRR next hops.",
            steps=[
                shell(
                    params.host_name,
                    "ip -j -4 route show table main | python3 -c "
                    + shlex.quote(script),
                    "Inspect dynamic FIB entries that send traffic toward endpoint links.",
                    Check(
                        "a dynamic FIB entry points toward an endpoint-facing link",
                        lambda out: bool(json.loads(out)),
                    ),
                    save_as="fib",
                ),
                OracleStep(
                    "Compare that prefix's selected RIB path with its installed FIB path.",
                    "exec_shell",
                    rib_args,
                    Check(
                        "the selected dynamic RIB next hops differ from the observed main-table entry",
                        lambda out: bool(
                            divergent_routes(rib_routes(out), observed["fib"])
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )
