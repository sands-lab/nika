"""Typed solved routing parameters and bindings for existing renderers."""

from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv4Network

from pydantic import Field, StrictInt, model_validator

from nika.net_env.contract import ContractModel
from nika.net_env.isp.bgp.policy import BgpPolicyTerm


class BgpExport(ContractModel):
    node: str
    neighbor: str
    terms: tuple[BgpPolicyTerm, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_neighbor(self):
        IPv4Address(self.neighbor)
        if any(term.local_preference is not None for term in self.terms):
            raise ValueError("Export completion only supports permit/deny decisions")
        if len({t.prefix for t in self.terms}) != len(self.terms):
            raise ValueError("Export prefixes must be distinct")
        return self


class OspfCost(ContractModel):
    node: str
    interface: str = Field(pattern=r"^eth[0-9]+$")
    cost: StrictInt = Field(ge=1, le=65535)


class BgpImport(ContractModel):
    node: str
    neighbor: str
    terms: tuple[BgpPolicyTerm, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def policy(self):
        IPv4Address(self.neighbor)
        if len({t.prefix for t in self.terms}) != len(self.terms):
            raise ValueError("Import prefixes must be distinct")
        if any(t.permit != (t.local_preference is not None) for t in self.terms):
            raise ValueError("Permitted imports require a solved local preference")
        return self


class IsisMetric(ContractModel):
    """Wide L2 metric restricted to the shared solver's positive 16-bit domain."""

    node: str
    interface: str = Field(pattern=r"^eth[0-9]+$")
    metric: StrictInt = Field(ge=1, le=65535)


class ImportPreference(ContractModel):
    node: str
    interface: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    prefix: str | None = None
    permit: bool = True
    local_preference: StrictInt | None = Field(default=None, ge=1, le=4294967295)

    @model_validator(mode="after")
    def policy(self):
        if self.prefix is not None:
            IPv4Network(self.prefix)
        if self.permit != (self.local_preference is not None):
            raise ValueError(
                "Permitted imports need a preference; denied imports have no preference"
            )
        if self.prefix is None and not self.permit:
            raise ValueError("Whole-interface completion only supports a preference")
        return self


class RoutingParameters(ContractModel):
    bgp_exports: tuple[BgpExport, ...] = ()
    bgp_imports: tuple[BgpImport, ...] = ()
    ospf_costs: tuple[OspfCost, ...] = ()
    isis_metrics: tuple[IsisMetric, ...] = ()
    import_preferences: tuple[ImportPreference, ...] = ()

    @model_validator(mode="after")
    def distinct_targets(self):
        for items, field in (
            (self.bgp_exports, "neighbor"),
            (self.bgp_imports, "neighbor"),
            (self.ospf_costs, "interface"),
            (self.isis_metrics, "interface"),
            (self.import_preferences, "interface"),
        ):
            keys = [
                (i.node, getattr(i, field), getattr(i, "prefix", None)) for i in items
            ]
            if len(keys) != len(set(keys)):
                raise ValueError("Solved parameters must have distinct targets")
        return self


def apply_routing_parameters(env, parameters: dict | RoutingParameters | None):
    """Render solved values while preserving protocol, service and failure targets."""
    if parameters is None:
        return
    solved = RoutingParameters.model_validate(parameters)
    supplied = {name for name, values in solved if values}
    if not supplied <= env.ROUTING_PARAMETER_KINDS:
        raise ValueError("Solved parameter kinds do not match scenario capabilities")
    if solved.isis_metrics:
        raise ValueError(
            "IS-IS metric parameters must bind through the ISP plan compiler"
        )
    for node in {
        p.node
        for group in (solved.bgp_exports, solved.ospf_costs, solved.import_preferences)
        for p in group
    }:
        if node not in env.lab.machines:
            raise ValueError(f"Unknown solved routing node: {node}")
        machine = env.lab.machines[node]
        config = machine.fs.readtext("etc/frr/frr.conf")
        for index, policy in enumerate(p for p in solved.bgp_exports if p.node == node):
            if not re.search(
                rf"^\s*neighbor {re.escape(policy.neighbor)} remote-as [0-9]+$",
                config,
                re.MULTILINE,
            ):
                raise ValueError(f"Unknown BGP neighbor on {node}: {policy.neighbor}")
            if re.search(
                rf"^\s*neighbor {re.escape(policy.neighbor)} route-map \S+ out$",
                config,
                re.MULTILINE,
            ):
                raise ValueError("Completion cannot replace an existing export policy")
            asn = re.search(r"^router bgp ([0-9]+)$", config, re.MULTILINE)
            if asn is None:
                raise ValueError("Expected one default-VRF BGP instance")
            statements = []
            for seq, term in enumerate(policy.terms, 1):
                plist = f"NC-{index}-{seq}"
                statements += [
                    f"ip prefix-list {plist} seq 5 permit {term.prefix}",
                    f"route-map NC-OUT-{index} {'permit' if term.permit else 'deny'} {seq * 10}",
                    f" match ip address prefix-list {plist}",
                    "!",
                ]
            statements += [
                f"router bgp {asn[1]}",
                f" neighbor {policy.neighbor} route-map NC-OUT-{index} out",
                "!",
            ]
            config += "\n" + "\n".join(statements) + "\n"
        for cost in (p for p in solved.ospf_costs if p.node == node):
            if int(cost.interface.removeprefix("eth")) not in machine.interfaces:
                raise ValueError("Unknown OSPF interface")
            config += f"\ninterface {cost.interface}\n ip ospf cost {cost.cost}\n!\n"
        interfaces = sorted(
            {p.interface for p in solved.import_preferences if p.node == node}
        )
        for interface in interfaces:
            policies = [
                p
                for p in solved.import_preferences
                if p.node == node and p.interface == interface
            ]
            pattern = rf"(route-map RM-IN-{re.escape(interface)} permit 10\n match ip address prefix-list ENTERPRISE\n set local-preference )[0-9]+"
            if len(re.findall(pattern, config)) != 1:
                raise ValueError("Expected one existing tunnel import policy")
            if any(p.prefix is None for p in policies):
                if len(policies) != 1:
                    raise ValueError(
                        "Cannot mix prefix-specific and whole-interface imports"
                    )
                config = re.sub(
                    pattern, lambda m: m[1] + str(policies[0].local_preference), config
                )
            else:
                # Keep the existing policy for prefixes outside the declared
                # completion scope. Exact-prefix rules precede that fallback.
                config = config.replace(
                    f"route-map RM-IN-{interface} permit 10\n",
                    f"route-map RM-IN-{interface} permit 65535\n",
                    1,
                )
                for seq, policy in enumerate(
                    sorted(policies, key=lambda p: p.prefix), 1
                ):
                    name = f"NC-IN-{interface}-{seq}"
                    config += f"\nip prefix-list {name} seq 5 permit {policy.prefix}\nroute-map RM-IN-{interface} {'permit' if policy.permit else 'deny'} {seq * 10}\n match ip address prefix-list {name}\n"
                    if policy.permit:
                        config += f" set local-preference {policy.local_preference}\n"
                    config += "!\n"
        machine.create_file_from_string(config, "/etc/frr/frr.conf")


def bind_isp_metrics(
    config,
    plan,
    bgp_plan,
    parameters,
    *,
    topology=None,
    parameter_kinds=frozenset({"ospf_costs", "isis_metrics"}),
):
    """Bind complete symmetric metric holes before either vendor renders configs."""
    from dataclasses import replace
    from nika.net_env.isp.igp.plan import active_igp_links, compile_isp_plan
    from nika.net_env.isp.bgp import scope_igp_to_bgp_as

    if parameters is None:
        return plan
    solved = RoutingParameters.model_validate(parameters)
    if not {name for name, values in solved if values} <= parameter_kinds:
        raise ValueError("Solved parameter kinds do not match ISP capabilities")
    if solved.ospf_costs and solved.isis_metrics:
        raise ValueError("A routing sketch must retain one IGP")
    if not (solved.ospf_costs or solved.isis_metrics):
        return plan
    if config.igp != ("isis" if solved.isis_metrics else "ospf"):
        raise ValueError("ISP metric completion requires a matching IGP sketch")
    costs = {(c.node, c.interface): c.cost for c in solved.ospf_costs}
    costs.update({(c.node, c.interface): c.metric for c in solved.isis_metrics})
    active = active_igp_links(plan)
    expected = {
        (n, i)
        for e in active
        for n, i in [(e.endpoint_a, e.iface_a), (e.endpoint_b, e.iface_b)]
    }
    if set(costs) != expected:
        raise ValueError(
            "ISP completion must bind every active IGP backbone interface exactly once"
        )
    metrics = {e.link_id: e.metric for e in plan.links}
    for edge in active:
        cost = costs[edge.endpoint_a, edge.iface_a]
        if cost != costs[edge.endpoint_b, edge.iface_b]:
            raise ValueError("ISP completion requires symmetric IGP costs")
        metrics[edge.link_id] = cost
    return scope_igp_to_bgp_as(
        compile_isp_plan(replace(config, link_metrics=metrics), topology=topology),
        bgp_plan,
    )


def bind_isp_imports(plan, parameters):
    """Bind AS-wide policies to fixed eBGP sessions before rendering."""
    from dataclasses import replace

    solved = RoutingParameters.model_validate(parameters)
    if not solved.bgp_imports:
        return plan
    if plan is None or plan.mode != "ebgp":
        raise ValueError("Import completion requires a fixed eBGP session sketch")
    from nika.net_env.isp.bgp.policy import fixed_policy_prefixes

    protected = fixed_policy_prefixes(plan)
    if any(t.prefix in protected for p in solved.bgp_imports for t in p.terms):
        raise ValueError(
            "Import completion cannot replace fixed security prefix policies"
        )
    sessions = {
        (s.local_device, s.remote_ip) for s in plan.sessions if s.session_type == "ebgp"
    }
    if any((p.node, p.neighbor) not in sessions for p in solved.bgp_imports):
        raise ValueError("Solved import policy references an unknown eBGP session")
    policies = {(p.node, p.neighbor): p.terms for p in solved.bgp_imports}
    groups = {}
    for s in plan.sessions:
        if s.session_type == "ebgp":
            groups.setdefault((s.local_asn, s.remote_asn), []).append(
                policies.get((s.local_device, s.remote_ip))
            )
    if any(
        any(terms) and any(t != terms[0] for t in terms) for terms in groups.values()
    ):
        raise ValueError("AS import policies must cover all borders consistently")
    return replace(
        plan,
        nodes=tuple(
            replace(
                n,
                import_policies=tuple(
                    (p.neighbor, p.terms)
                    for p in solved.bgp_imports
                    if p.node == n.device_name
                ),
            )
            for n in plan.nodes
        ),
    )
