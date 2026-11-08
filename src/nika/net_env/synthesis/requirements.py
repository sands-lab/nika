"""Resolve routing behavior once for synthesis and independent route checks."""

from __future__ import annotations

from collections import defaultdict
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Annotated, Literal

from pydantic import Field, model_validator

from nika.net_env.contract import (
    BgpRouteExpectation,
    ContractModel,
    RouteNextHopsExpectation,
    ValidationContract,
    ValidationIntent,
)


class BgpImportPeer(ContractModel):
    """A neighbor in the fixed addressing/session sketch, before completion."""

    node: str
    interface: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    local_asn: int = Field(ge=1, le=4294967295)
    remote_asn: int = Field(ge=1, le=4294967295)
    remote_address: str

    @model_validator(mode="after")
    def address(self):
        IPv4Address(self.remote_address)
        if self.local_asn == self.remote_asn:
            raise ValueError(
                "The local import completion model requires eBGP neighbors"
            )
        return self


class OspfPathRequirement(ContractModel):
    kind: Literal["ospf_path"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    area: int = Field(ge=0, le=4294967295)
    prefix: str
    path: tuple[str, ...] = Field(min_length=2)
    state_paths: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def paths(self):
        IPv4Network(self.prefix)
        for path in [self.path, *self.state_paths.values()]:
            if len(path) < 2 or len(set(path)) != len(path):
                raise ValueError("Routing paths must be simple paths")
            if (path[0], path[-1]) != (self.path[0], self.path[-1]):
                raise ValueError("State paths must preserve the original endpoints")
        return self

    def path_in(self, state: str):
        return self.state_paths.get(state, self.path)


class LinkStatePathsRequirement(ContractModel):
    """Exact allowed link-state paths to one connected prefix, including ECMP.

    Each nonterminal suffix also defines a route requirement. State paths may
    start at those routers to retain their requirements after anchor isolation.
    """

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    prefix: str
    paths: tuple[tuple[str, ...], ...] = Field(min_length=1)
    # An empty state group means the declared sources are disconnected.
    state_paths: dict[str, tuple[tuple[str, ...], ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_paths(self):
        IPv4Network(self.prefix)
        terminal = self.paths[0][-1] if self.paths[0] else None
        sources = {node for path in self.paths for node in path[:-1]}
        for paths in (self.paths, *self.state_paths.values()):
            if len(paths) != len(set(paths)):
                raise ValueError("Link-state paths must be distinct")
            for path in paths:
                if len(path) < 2 or len(set(path)) != len(path):
                    raise ValueError("Routing paths must be simple paths")
                if path[-1] != terminal or path[0] not in sources:
                    raise ValueError("State paths must preserve the original endpoints")
        return self

    def paths_in(self, state: str):
        return self.state_paths.get(state, self.paths)


class OspfPathsRequirement(LinkStatePathsRequirement):
    kind: Literal["ospf_paths"]
    area: int = Field(ge=0, le=4294967295)


class IsisPathsRequirement(LinkStatePathsRequirement):
    kind: Literal["isis_paths"]
    level: Literal[2] = 2


def link_state_paths_intents(requirement, paths, adjacencies):
    """Bind permitted router edges to every physical next hop in the sketch."""
    neighbors = defaultdict(set)
    for path in paths:
        for node, following in zip(path, path[1:]):
            found = [
                a
                for a in adjacencies
                if (a.local_node, a.remote_node) == (node, following)
            ]
            if not found:
                raise ValueError("Routing path traverses an undeclared adjacency")
            if isinstance(requirement, OspfPathRequirement) and len(found) != 1:
                raise ValueError(
                    "Single path behavior requires unambiguous router-pair adjacencies"
                )
            neighbors[node].update(a.remote_address for a in found)
    return [
        ValidationIntent(
            id=f"{requirement.id}.{node}",
            description=f"Forward the original prefix over exactly its declared {'IS-IS' if isinstance(requirement, IsisPathsRequirement) else 'OSPF'} paths.",
            property="route_next_hops",
            expected="next_hops_match",
            route_next_hops=RouteNextHopsExpectation(
                node=node, prefix=requirement.prefix, next_hops=tuple(sorted(hops))
            ),
        )
        for node, hops in sorted(neighbors.items())
    ]


class BgpSelectionRequirement(ContractModel):
    kind: Literal["bgp_selection"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    node: str
    prefix: str
    # Ordered logical interfaces, resolved against the original session sketch.
    candidates: tuple[str, ...] = Field(min_length=1)
    unavailable: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def selection(self):
        IPv4Network(self.prefix)
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("BGP candidates must be distinct")
        for unavailable in self.unavailable.values():
            if len(set(unavailable)) != len(unavailable) or not set(unavailable) < set(
                self.candidates
            ):
                raise ValueError(
                    "A state must leave at least one declared BGP candidate"
                )
        return self

    def selected_in(self, state: str):
        unavailable = set(self.unavailable.get(state, ()))
        return next(c for c in self.candidates if c not in unavailable)


class BgpAsSelectionRequirement(ContractModel):
    """Ordered neighboring ASes, shared by every border of one routing domain."""

    kind: Literal["bgp_as_selection"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    asn: int = Field(ge=1, le=4294967295)
    prefix: str
    candidates: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_candidates(self):
        IPv4Network(self.prefix)
        if len(set(self.candidates)) != len(self.candidates) or any(
            not 1 <= c <= 4294967295 or c == self.asn for c in self.candidates
        ):
            raise ValueError("BGP candidates must be distinct neighboring ASNs")
        return self


def default_as_selections(plan) -> tuple[BgpAsSelectionRequirement, ...]:
    """Declare shortest-AS-distance preferences from the original session graph.

    ASN order breaks equal-distance ties as an explicit experimental policy.
    A backup must reach the origin without returning through the local AS.
    Security-controlled prefixes retain their original policy semantics.
    """
    from nika.net_env.isp.bgp.policy import fixed_policy_prefixes
    from nika.net_env.isp.igp.plan import shortest_distances

    if plan is None or plan.mode != "ebgp":
        return ()
    graph = defaultdict(set)
    for session in plan.sessions:
        if session.session_type == "ebgp":
            graph[session.local_asn].add(session.remote_asn)
    owners = defaultdict(set)
    for node in plan.nodes:
        for origin in node.originated:
            owners[origin.prefix].add(node.asn)
    requirements = []
    for prefix, asns in sorted(owners.items()):
        if prefix in fixed_policy_prefixes(plan) or len(asns) != 1:
            continue
        (owner,) = asns
        for local in sorted(graph):
            if local == owner:
                continue
            remaining = {
                a: [(b, 1) for b in sorted(neighbors) if b != local]
                for a, neighbors in graph.items()
                if a != local
            }
            distances = shortest_distances(remaining, owner)
            candidates = tuple(
                sorted(
                    (a for a in graph[local] if a in distances),
                    key=lambda a: (distances[a], a),
                )
            )
            if not candidates:
                raise ValueError(
                    "Original BGP sketch has no candidate route to its origin AS"
                )
            requirements.append(
                BgpAsSelectionRequirement(
                    kind="bgp_as_selection",
                    id=f"exit.as{local}.{prefix.replace('/', '-')}",
                    asn=local,
                    prefix=prefix,
                    candidates=candidates,
                )
            )
    return tuple(requirements)


RoutingRequirement = Annotated[
    OspfPathRequirement
    | OspfPathsRequirement
    | IsisPathsRequirement
    | BgpSelectionRequirement
    | BgpAsSelectionRequirement,
    Field(discriminator="kind"),
]


def resolve_routing_requirements(spec, env):
    """Project original path/selection semantics into existing typed predicates.

    Address and interface bindings come from the fixed sketch, never the solver
    output, generated route policies or observed routing state.
    """
    if not spec.behavior:
        return spec
    state_names = [
        "healthy",
        *(i.name for i in spec.incidents),
        *(f.name for f in spec.failure_checks),
    ]
    if len(state_names) != len(set(state_names)):
        raise ValueError(
            "Operating state names must be unique and distinct from healthy"
        )
    states = {
        "healthy",
        *(i.name for i in spec.incidents),
        *(f.name for f in spec.failure_checks if f.requirements),
    }
    ids = [r.id for r in spec.behavior]
    if len(ids) != len(set(ids)):
        raise ValueError("Routing behavior IDs must be distinct")
    authored = [spec.healthy, spec.runtime_requirements] + [
        c for i in spec.incidents for c in (i.requirements, i.runtime_requirements)
    ]
    authored += [f.requirements for f in spec.failure_checks if f.requirements]
    if any(
        intent.id == name or intent.id.startswith(name + ".")
        for contract in authored
        if contract
        for intent in contract.intents
        for name in ids
    ):
        raise ValueError(
            "Routing behavior cannot duplicate manually authored predicates"
        )
    peers = (
        {(p.node, p.interface): p for p in env.bgp_import_peers()}
        if any(isinstance(r, BgpSelectionRequirement) for r in spec.behavior)
        else {}
    )
    requirements = {name: [] for name in states}
    for requirement in spec.behavior:
        if isinstance(requirement, (OspfPathRequirement, LinkStatePathsRequirement)):
            from nika.validation.batfish.kathara import startup_addresses

            terminal = (
                requirement.path
                if isinstance(requirement, OspfPathRequirement)
                else requirement.paths[0]
            )[-1]
            if getattr(env, "device_profile", None) == "nokia_srlinux":
                from nika.validation.batfish.srl_isp import srl_addresses

                addresses = srl_addresses(env.routing_documents[terminal])
            elif getattr(env, "device_profile", None) == "sonic":
                from nika.validation.batfish.sonic import design_addresses

                addresses = design_addresses(env.sonic_routers[terminal])
            elif terminal in getattr(env, "native_routers", {}):
                from nika.validation.batfish.vendor_bgp import vendor_addresses

                addresses = vendor_addresses(
                    env.deployment_configs[terminal], env.native_routers[terminal]
                )
            elif hasattr(env, "BGP_ROUTERS") and getattr(
                env, "device_profile", None
            ) in {"iosxr", "routeros"}:
                from nika.validation.batfish.vendor_bgp import (
                    vendor_addresses,
                    vendor_configs,
                )

                addresses = vendor_addresses(
                    vendor_configs(env)[terminal], env.device_profile
                )
            else:
                if terminal not in env.lab.machines:
                    raise ValueError("Routing path has an unknown terminal router")
                addresses = startup_addresses(
                    env.lab.fs.readtext(f"{terminal}.startup"), include_loopback=True
                )
            if not any(
                IPv4Interface(address).network == IPv4Network(requirement.prefix)
                for address, _ in addresses
            ):
                raise ValueError(
                    "Routing path must terminate at its declared connected prefix"
                )
        declared_states = set(
            requirement.state_paths
            if isinstance(requirement, (OspfPathRequirement, LinkStatePathsRequirement))
            else requirement.unavailable
            if isinstance(requirement, BgpSelectionRequirement)
            else {}
        )
        if not declared_states <= states - {"healthy"}:
            raise ValueError("Routing behavior references an undeclared incident")
        for state in states:
            if isinstance(
                requirement, (OspfPathRequirement, LinkStatePathsRequirement)
            ):
                paths = (
                    (requirement.path_in(state),)
                    if isinstance(requirement, OspfPathRequirement)
                    else requirement.paths_in(state)
                )
                contract = (
                    spec.healthy
                    if state == "healthy"
                    else next(
                        i.requirements
                        for i in (*spec.incidents, *spec.failure_checks)
                        if i.name == state
                    )
                )
                state_adjacencies = [
                    i.adjacency
                    for i in contract.intents
                    if i.adjacency
                    and i.adjacency.protocol
                    == (
                        "isis"
                        if isinstance(requirement, IsisPathsRequirement)
                        else "ospf"
                    )
                ]
                requirements[state].extend(
                    link_state_paths_intents(requirement, paths, state_adjacencies)
                )
            elif isinstance(requirement, BgpAsSelectionRequirement):
                from nika.net_env.isp.bgp.policy import fixed_policy_prefixes

                plan = env.bgp_plan
                if plan is None or requirement.asn not in {n.asn for n in plan.nodes}:
                    raise ValueError("BGP selection requires a declared local AS")
                if requirement.prefix in fixed_policy_prefixes(plan):
                    raise ValueError(
                        "AS selection cannot replace fixed security prefix policies"
                    )
                owner_asns = {
                    n.asn
                    for n in plan.nodes
                    for o in n.originated
                    if o.prefix == requirement.prefix
                }
                if len(owner_asns) != 1 or requirement.asn in owner_asns:
                    raise ValueError(
                        "AS selection requires one remote-owned original prefix"
                    )
                neighbors = {
                    s.remote_asn
                    for s in plan.sessions
                    if s.local_asn == requirement.asn and s.session_type == "ebgp"
                }
                if not set(requirement.candidates) <= neighbors:
                    raise ValueError(
                        "BGP selection references an unknown neighboring AS"
                    )
                requirements[state].extend(
                    ValidationIntent(
                        id=f"{requirement.id}.{node.device_name}",
                        description="Select the declared neighboring AS for the original prefix.",
                        property="bgp_route",
                        expected="selected",
                        bgp_route=BgpRouteExpectation(
                            node=node.device_name,
                            prefix=requirement.prefix,
                            exit_asn=requirement.candidates[0],
                        ),
                    )
                    for node in plan.nodes
                    if node.asn == requirement.asn
                )
            else:
                if any(
                    (requirement.node, c) not in peers for c in requirement.candidates
                ):
                    raise ValueError(
                        "BGP selection references an unknown sketch neighbor"
                    )
                selected = peers[requirement.node, requirement.selected_in(state)]
                requirements[state].append(
                    ValidationIntent(
                        id=requirement.id,
                        description="Select the first available neighbor in the original prefix-specific ordering.",
                        property="route_next_hops",
                        expected="next_hops_match",
                        route_next_hops=RouteNextHopsExpectation(
                            node=requirement.node,
                            prefix=requirement.prefix,
                            next_hops=(selected.remote_address,),
                        ),
                    )
                )

    def extend(contract, generated, *, runtime_only=False):
        if contract is None:
            contract = ValidationContract(
                contract_id=spec.healthy.contract_id + ".routing-runtime",
                scenario=spec.scenario,
                design_source={
                    "basis": "Original routing behavior resolved against the fixed sketch",
                    "runtime_only": runtime_only,
                },
                intents=(),
            )
        # Validate the combined contract, including collisions with authored IDs.
        return ValidationContract.model_validate(
            {
                **contract.model_dump(),
                "intents": sorted([*contract.intents, *generated], key=lambda i: i.id),
            }
        )

    runtime_only = spec.routing_model == "linux-static"
    healthy = (
        spec.healthy if runtime_only else extend(spec.healthy, requirements["healthy"])
    )
    extra = (
        extend(spec.runtime_requirements, requirements["healthy"], runtime_only=True)
        if runtime_only
        else spec.runtime_requirements
    )
    incidents = []
    for incident in spec.incidents:
        common = (
            incident.requirements
            if runtime_only
            else extend(incident.requirements, requirements[incident.name])
        )
        additional = (
            extend(
                incident.runtime_requirements,
                requirements[incident.name],
                runtime_only=True,
            )
            if runtime_only
            else incident.runtime_requirements
        )
        incidents.append(
            incident.model_copy(
                update={"requirements": common, "runtime_requirements": additional}
            )
        )
    failures = tuple(
        f.model_copy(
            update={"requirements": extend(f.requirements, requirements[f.name])}
        )
        if f.requirements
        else f
        for f in spec.failure_checks
    )
    return spec.model_copy(
        update={
            "failure_checks": failures,
            "healthy": healthy,
            "runtime_requirements": extra,
            "incidents": tuple(incidents),
        }
    )
