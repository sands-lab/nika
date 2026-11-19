"""Origin-preserving BGP export-policy violation."""

import json
import re
from functools import lru_cache

from pydantic import BaseModel, Field, IPvAnyAddress

from nika.net_env.isp.bgp.config import EBGP_BASE_ASN
from nika.net_env.isp.bgp.relationships import transit_export_map
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import loaded_inventory
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.oracle import Check, OraclePlaybook, contains, shell, vtysh


def leak_sessions(bgp):
    """Customer exports to a provider whose peer is the legitimate origin AS."""
    return sorted(
        (
            row
            for row in bgp.get("inter_as_roles", ())
            if row["role"] == "provider" and row["remote_asn"] == EBGP_BASE_ASN + 2
        ),
        key=lambda row: (row["node"], row["neighbor"]),
    )


@lru_cache(maxsize=None)
def admits_route_leak(scenario):
    from nika.net_env.isp.bgp import compile_bgp_plan
    from nika.net_env.isp.identity import is_isp_base_topology, isp_topo_from_scenario
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan

    if not is_isp_base_topology(scenario):
        return False
    plan = compile_isp_plan(IspConfig(topology=isp_topo_from_scenario(scenario)))
    return bool(leak_sessions(compile_bgp_plan(plan, "ebgp", rpki=True).inventory))


def learned_origin_path(output, *, leaker_asn, origin_asn, require_valid=False):
    """Read real AS_PATHs; a changed origin or an unreadable table is not a leak."""
    try:
        paths = json.loads(output).get("paths", [])
    except (ValueError, AttributeError):
        return False
    for path in paths:
        aspath = (path.get("aspath") or {}).get("string", "").split()
        if aspath[:1] != [str(leaker_asn)] or aspath[-1:] != [str(origin_asn)]:
            continue
        if (
            require_valid
            and str(path.get("rpkiValidationState", "")).lower() != "valid"
        ):
            continue
        if (path.get("bestpath") or {}).get("overall"):
            return True
    return False


class BGPRouteLeakParams(BaseModel):
    host_name: str = Field(
        description="Customer border router with the incorrect export policy."
    )
    peer: IPvAnyAddress | None = Field(
        default=None, description="Provider neighbor receiving the leaked routes."
    )


class BGPRouteLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "bgp_route_leak"
    description = (
        "A BGP export policy permits provider-learned routes toward another provider."
    )
    symptom_desc = "Business traffic takes an unexpected inter-AS transit path."
    symptom_probe = "custom"
    symptom_class = "control_plane"
    effect_protocol = "bgp"
    CONFUSABLE_WITH = ("bgp_hijacking", "bgp_session_policy_missing")
    TAGS = ["bgp", "isp", "ebgp"]
    supported_backends = ("kathara",)
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": True}
    BENCHMARK_TARGETS = "canonical"
    Params = BGPRouteLeakParams

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        return (
            device_profile in (None, "", "frr")
            and super().is_compatible(target, device_profile, backend=backend)
            and admits_route_leak(target.partition("/")[0])
        )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        bgp = loaded_inventory(ctx.net_env).get("bgp", {})
        sessions = leak_sessions(bgp)
        if not sessions:
            raise ValueError(
                "The AS partition has no provider-customer-provider leak path."
            )
        row = sessions[0]
        return {"host_name": row["node"], "peer": row["neighbor"]}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        sessions = leak_sessions(loaded_inventory(ctx.net_env).get("bgp", {}))
        if not any(
            row["node"] == inject.get("host_name")
            and (not inject.get("peer") or row["neighbor"] == str(inject["peer"]))
            for row in sessions
        ):
            raise ValueError("Target is not a declared upstream export session.")

    def session(self, params):
        bgp = self.net_env.inventory["bgp"]
        matches = [
            row
            for row in leak_sessions(bgp)
            if row["node"] == params.host_name
            and (params.peer is None or row["neighbor"] == str(params.peer))
        ]
        if not matches:
            raise ValueError("Target is not a declared upstream export session.")
        return matches[0]

    def root_cause_resources(self, params):
        self.session(params)
        return [node_resource(params.host_name)]

    def business_flow(self):
        bgp = self.net_env.inventory["bgp"]
        asns = {row["device"]: row["asn"] for row in bgp["nodes"]}
        source = next(
            row for row in bgp["originated"] if asns[row["device"]] == EBGP_BASE_ASN + 2
        )
        return source["device"], source["ping_address"], "198.51.100.1"

    def _config(self, host):
        return self.runtime.exec(host, vtysh("show running-config"))

    @staticmethod
    def _binding(config, neighbor):
        match = re.search(
            rf"^\s*neighbor {re.escape(neighbor)} route-map (\S+) out$", config, re.M
        )
        return match[1] if match else None

    def _set_binding(self, params, binding):
        session = self.session(params)
        self.runtime.exec(
            params.host_name,
            frr_config_command(
                f"router bgp {EBGP_BASE_ASN + 1}",
                "address-family ipv4 unicast",
                f"neighbor {session['neighbor']} route-map {binding} out",
            ),
        )
        frr_save_config(self.runtime, params.host_name)
        self.runtime.exec(
            params.host_name, vtysh(f"clear bgp {session['neighbor']} soft out")
        )

    def inject_fault(self, params):
        session = self.session(params)
        config = self._config(params.host_name)
        original = self._binding(config, session["neighbor"])
        if original != transit_export_map(session["neighbor"]):
            raise RuntimeError("Expected the healthy upstream transit export policy.")
        match = re.search(
            rf"^route-map {re.escape(original)} permit 10\n(.*?)(?=^!)",
            config,
            re.M | re.S,
        )
        called = re.search(r"^\s*call (\S+)$", match[1], re.M) if match else None
        if called is None:
            raise RuntimeError("Missing ordinary export policy.")
        self._original_export = original
        self._ordinary_export = called[1]
        self._set_binding(params, self._ordinary_export)

    def verify_fault(self, params):
        session = self.session(params)
        observed = self._binding(self._config(params.host_name), session["neighbor"])
        expected = getattr(self, "_ordinary_export", None)
        return build_verify_result(
            self.root_cause_name,
            bool(expected and observed == expected),
            {
                "node": params.host_name,
                "neighbor": session["neighbor"],
                "export": observed,
            },
        )

    def recover_fault(self, params):
        original = getattr(self, "_original_export", None)
        if original is None:
            return build_verify_result(
                self.root_cause_name,
                False,
                {"error": "Missing export policy snapshot."},
            )
        self._set_binding(params, original)
        restored = (
            self._binding(
                self._config(params.host_name), self.session(params)["neighbor"]
            )
            == original
        )
        return build_verify_result(self.root_cause_name, restored, {})

    def oracle_playbook(self, params):
        session = self.session(params)
        bgp = self.net_env.inventory["bgp"]
        prefix = "198.51.100.0/24"
        observer = bgp["rov_observer"]
        source, _source_ip, destination = self.business_flow()
        expected = Check(
            "the best route retains the authorized origin and traverses the customer AS with RPKI valid",
            lambda out: learned_origin_path(
                out,
                leaker_asn=EBGP_BASE_ASN + 1,
                origin_asn=EBGP_BASE_ASN,
                require_valid=True,
            ),
        )
        return OraclePlaybook(
            finding=f"{params.host_name} exports a provider-learned route to {session['neighbor']}, preserving its valid origin AS.",
            steps=[
                shell(
                    observer,
                    vtysh(f"show bgp ipv4 unicast {prefix} json"),
                    "Check the best AS path and RPKI origin validation; distinguish a leak from origin hijacking or withdrawal.",
                    expected,
                    evidence=True,
                    attempts=20,
                    interval_sec=3,
                ),
                shell(
                    params.host_name,
                    vtysh(f"show bgp ipv4 unicast {prefix}"),
                    "Verify the leaker learned the route from another AS.",
                    contains(str(EBGP_BASE_ASN)),
                ),
                shell(
                    params.host_name,
                    vtysh("show running-config"),
                    "Compare the upstream export binding with the local-origin-only transit policy.",
                    Check(
                        "the upstream bypasses its transit restriction",
                        lambda out: (
                            self._binding(out, session["neighbor"])
                            not in (None, transit_export_map(session["neighbor"]))
                        ),
                    ),
                    evidence=True,
                ),
                shell(
                    source,
                    vtysh(f"show ip route {destination}"),
                    "Inspect the business destination's selected next hop.",
                ),
            ],
        )
