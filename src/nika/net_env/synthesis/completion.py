"""Lower declared routing sketches to NetComplete and bind solved parameters."""

from __future__ import annotations

import re
from collections import defaultdict
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from nika.net_env.contract import ContractModel
from nika.net_env.isp.bgp.policy import BgpPolicyTerm
from nika.net_env.synthesis.parameters import (
    BgpExport,
    BgpImport,
    ImportPreference,
    OspfCost,
    IsisMetric,
    RoutingParameters,
)
from nika.net_env.synthesis.runner import run_netcomplete
from nika.net_env.synthesis.requirements import (
    OspfPathRequirement,
    OspfPathsRequirement,
    IsisPathsRequirement,
    BgpSelectionRequirement,
    BgpAsSelectionRequirement,
    BgpImportPeer,
)
from nika.validation.batfish.kathara import startup_addresses


class BgpFabricCompletion(ContractModel):
    kind: Literal["bgp_fabric"]
    # Prefix ownership and all-router shortest-AS-path propagation are original inputs.
    origins: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_prefixes(self):
        for prefix in self.origins:
            IPv4Network(prefix)
        return self


class OspfAreaCompletion(ContractModel):
    kind: Literal["ospf_area"]
    area: int = Field(ge=0, le=4294967295)
    routers: tuple[str, ...] = Field(min_length=2)
    requirements: tuple[str, ...] = Field(min_length=1)
    parallel_links: Literal["reject", "equal_cost"] = "reject"

    @model_validator(mode="after")
    def valid_scope(self):
        if len(set(self.routers)) != len(self.routers):
            raise ValueError("OSPF routers must be distinct")
        return self


class IsisLevelCompletion(ContractModel):
    """Reuse positive-weight shortest-path synthesis for one IS-IS L2 domain."""

    kind: Literal["isis_level"]
    level: Literal[2] = 2
    routers: tuple[str, ...] = Field(min_length=2)
    requirements: tuple[str, ...] = Field(min_length=1)
    parallel_links: Literal["reject", "equal_cost"] = "reject"

    @model_validator(mode="after")
    def distinct_routers(self):
        if len(set(self.routers)) != len(self.routers):
            raise ValueError("IS-IS routers must be distinct")
        return self


class BgpImportCompletion(ContractModel):
    kind: Literal["bgp_imports"]
    requirements: tuple[str, ...] = Field(min_length=1)


class BgpAsImportCompletion(ContractModel):
    kind: Literal["bgp_as_imports"]
    requirements: tuple[str, ...] = Field(min_length=1)


RoutingCompletion = Annotated[
    BgpFabricCompletion
    | OspfAreaCompletion
    | IsisLevelCompletion
    | BgpImportCompletion
    | BgpAsImportCompletion,
    Field(discriminator="kind"),
]


def complete_routing(spec, env, directory: Path, *, image: str):
    completion = spec.completion
    if isinstance(completion, tuple):
        if not completion:
            raise ValueError("A composed completion must contain at least one stage")
        solved = [
            complete_routing(
                spec.model_copy(update={"completion": stage}),
                env,
                directory / f"stage-{index}",
                image=image,
            )
            for index, stage in enumerate(completion)
        ]
        return RoutingParameters(
            **{
                name: tuple(item for result in solved for item in getattr(result, name))
                for name in RoutingParameters.model_fields
            }
        )
    if isinstance(completion, BgpAsImportCompletion):
        by_id = {r.id: r for r in spec.behavior}
        selected = [by_id.get(name) for name in completion.requirements]
        if len(set(completion.requirements)) != len(completion.requirements) or any(
            not isinstance(r, BgpAsSelectionRequirement) for r in selected
        ):
            raise ValueError(
                "AS import completion requires distinct original AS selections"
            )
        plan = env.bgp_plan
        if plan is None:
            raise ValueError("AS import completion requires a fixed BGP sketch")
        sessions = [s for s in plan.sessions if s.session_type == "ebgp"]
        addresses = {(s.local_asn, s.remote_asn): s.remote_ip for s in sessions}
        peers = [
            BgpImportPeer(
                node=f"as{local}",
                interface=f"as{remote}",
                local_asn=local,
                remote_asn=remote,
                remote_address=addresses[local, remote],
            )
            for local, remote in sorted({(s.local_asn, s.remote_asn) for s in sessions})
        ]
        requirements = [
            BgpSelectionRequirement(
                kind="bgp_selection",
                id=r.id,
                node=f"as{r.asn}",
                prefix=r.prefix,
                candidates=tuple(f"as{c}" for c in r.candidates),
            )
            for r in selected
        ]
        logical = _complete_imports(
            requirements, peers, directory, image, spec.solver_timeout_sec
        )
        grouped = defaultdict(list)
        for p in logical.import_preferences:
            grouped[p.node, p.interface].append(
                BgpPolicyTerm(
                    prefix=p.prefix,
                    permit=p.permit,
                    local_preference=p.local_preference,
                )
            )
        return RoutingParameters(
            bgp_imports=tuple(
                BgpImport(node=s.local_device, neighbor=s.remote_ip, terms=tuple(terms))
                for s in sorted(sessions, key=lambda s: (s.local_device, s.remote_ip))
                if (terms := grouped.get((f"as{s.local_asn}", f"as{s.remote_asn}")))
            )
        )
    adjacencies = [i.adjacency for i in spec.healthy.intents if i.adjacency]
    if isinstance(completion, BgpFabricCompletion):
        adjacencies = [a for a in adjacencies if a.protocol == "bgp"]
        routers = {}
        for adjacency in adjacencies:
            if (
                adjacency.local_node in routers
                and routers[adjacency.local_node] != adjacency.local_asn
            ):
                raise ValueError("Conflicting declared BGP ASNs")
            routers[adjacency.local_node] = adjacency.local_asn
        if not routers or not set(completion.origins.values()) <= set(routers):
            raise ValueError("BGP origins require declared device adjacencies")
        if len(set(routers.values())) != len(routers):
            raise ValueError("Device BGP completion requires distinct router ASNs")
        configured_origins = env.bgp_origin_prefixes()
        if configured_origins != completion.origins:
            raise ValueError(
                "Declared BGP origins must cover the candidate's origin sketch"
            )
        pairs = {(a.local_node, a.remote_node) for a in adjacencies}
        if any((b, a) not in pairs for a, b in pairs):
            raise ValueError(
                "Device-level synthesis requires both adjacency directions"
            )
        payload = {
            "kind": "bgp_graph",
            "routers": routers,
            "links": sorted({tuple(sorted(pair)) for pair in pairs}),
            "traffic": [
                dict(prefix=p, origin=n) for p, n in sorted(completion.origins.items())
            ],
        }
        result = run_netcomplete(
            payload, directory, image=image, timeout=spec.solver_timeout_sec
        )
        expected = {(a, b, p) for a, b in pairs for p in completion.origins}
        actual = [(r["node"], r["peer"], r["prefix"]) for r in result]
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("Incomplete device-level BGP solution")
        terms = defaultdict(list)
        addresses = {
            (a.local_node, a.remote_node): a.remote_address for a in adjacencies
        }
        for row in result:
            terms[row["node"], addresses[row["node"], row["peer"]]].append(
                BgpPolicyTerm(prefix=row["prefix"], permit=row["permit"])
            )
        return RoutingParameters(
            bgp_exports=tuple(
                BgpExport(
                    node=node,
                    neighbor=neighbor,
                    terms=tuple(sorted(values, key=lambda t: t.prefix)),
                )
                for (node, neighbor), values in sorted(terms.items())
            )
        )
    if isinstance(completion, (OspfAreaCompletion, IsisLevelCompletion)):
        isis = isinstance(completion, IsisLevelCompletion)
        protocol = "isis" if isis else "ospf"
        requirement_types = (
            (IsisPathsRequirement,)
            if isis
            else (OspfPathRequirement, OspfPathsRequirement)
        )
        by_id = {r.id: r for r in spec.behavior}
        selected = [by_id.get(name) for name in completion.requirements]
        if len(set(completion.requirements)) != len(completion.requirements) or any(
            not isinstance(r, requirement_types) for r in selected
        ):
            raise ValueError(
                "Link-state completion must reference distinct original path requirements"
            )
        for requirement in selected:
            all_paths = (
                (requirement.path, *requirement.state_paths.values())
                if isinstance(requirement, OspfPathRequirement)
                else (
                    *requirement.paths,
                    *(
                        path
                        for paths in requirement.state_paths.values()
                        for path in paths
                    ),
                )
            )
            if (not isis and requirement.area != completion.area) or any(
                not set(path) <= set(completion.routers) for path in all_paths
            ):
                raise ValueError("Link-state paths must stay within the declared area")
        states = {"healthy": spec.healthy}
        state_names = {state for r in selected for state in r.state_paths}
        states.update(
            {
                i.name: i.requirements
                for i in (*spec.incidents, *spec.failure_checks)
                if i.name in state_names and i.requirements
            }
        )
        if not state_names <= states.keys():
            raise ValueError("Link-state completion references an undeclared incident")
        links = {}
        interfaces = defaultdict(set)
        physical = set()
        for a in adjacencies:
            if (
                a.protocol != protocol
                or a.local_node not in completion.routers
                or a.remote_node not in completion.routers
            ):
                continue
            physical.add(
                (a.local_node, a.remote_node, a.local_address, a.remote_address)
            )
            srl = getattr(env, "device_profile", None) == "nokia_srlinux"
            vendor = getattr(env, "device_profile", None) in {"iosxr", "routeros"}
            if srl:
                from nika.validation.batfish.srl_isp import (
                    srl_addresses,
                    project_srl_isp,
                )

                document = env.routing_documents[a.local_node]
                project_srl_isp(a.local_node, document)
                addresses = [
                    (addr, "eth" + str(int(iface.split("/")[1].split(".")[0]) - 1))
                    for addr, iface in srl_addresses(document)
                    if iface.startswith("ethernet-")
                ]
            elif vendor:
                from nika.validation.batfish.vendor_bgp import (
                    vendor_addresses,
                    vendor_configs,
                    vendor_ospf_interfaces,
                )

                config = vendor_configs(env)[a.local_node]
                logical = {
                    native: name for name, native in env.BGP_INTERFACE_MAP.items()
                }
                addresses = [
                    (address, logical.get(interface, interface))
                    for address, interface in vendor_addresses(
                        config, env.device_profile
                    )
                ]
            else:
                addresses = startup_addresses(
                    env.lab.fs.readtext(f"{a.local_node}.startup")
                )
            found = [
                (IPv4Interface(addr), iface)
                for addr, iface in addresses
                if str(IPv4Interface(addr).ip) == a.local_address
            ]
            if len(found) != 1:
                raise ValueError(
                    "Link-state address does not identify one concrete interface"
                )
            address, interface = found[0]
            if srl:
                from nika.net_env.isp.igp.ifaces import srl_subinterface

                protocols = document["srl_nokia-network-instance:network-instance"][0][
                    "protocols"
                ]
                igp = protocols[
                    "srl_nokia-isis:isis" if isis else "srl_nokia-ospf:ospf"
                ]["instance"][0]
                if not isis:
                    (area,) = igp["area"]
                    if int(IPv4Address(area["area-id"])) != completion.area:
                        raise ValueError(
                            "Link-state area differs from completion sketch"
                        )
                rows = igp["interface"] if isis else area["interface"]
                (row,) = [
                    r
                    for r in rows
                    if r["interface-name"] == srl_subinterface(interface)
                ]
                if row.get("passive"):
                    raise ValueError("Link-state completion requires active interfaces")
            elif vendor:
                if isis:
                    raise ValueError("Native ISP completion supports OSPF only")
                binding = vendor_ospf_interfaces(
                    a.local_node, config, env.device_profile
                ).get(env.BGP_INTERFACE_MAP[interface])
                if binding != (completion.area, False):
                    raise ValueError(
                        "Link-state completion requires active native interfaces in the declared area"
                    )
            else:
                config = env.lab.machines[a.local_node].fs.readtext("etc/frr/frr.conf")
                if isis:
                    from nika.validation.batfish.isis import parse_frr_isis

                    from nika.validation.batfish.frr_ibgp import (
                        project_frr_ibgp,
                        split_frr_ibgp,
                    )

                    igp_config, bgp_fragment = split_frr_ibgp(config)
                    project_frr_ibgp(
                        bgp_fragment,
                        allow_ebgp=env.bgp_plan is not None
                        and env.bgp_plan.mode == "ebgp",
                    )
                    _, configured = parse_frr_isis(a.local_node, igp_config)
                    iface = configured.get(interface)
                    if (
                        iface is None
                        or not iface["enabled"]
                        or iface["address"] is None
                        or iface["passive"]
                        or str(iface["address"].ip) != a.local_address
                    ):
                        raise ValueError(
                            "IS-IS completion requires active L2 IPv4 interfaces"
                        )
                else:
                    declarations = re.findall(
                        r"^\s*network ([0-9./]+) area ([0-9.]+)$", config, re.MULTILINE
                    )
                    areas = {
                        int(IPv4Address(area)) if "." in area else int(area)
                        for network, area in declarations
                        if address.ip in IPv4Network(network)
                    }
                    if areas != {completion.area}:
                        raise ValueError(
                            "Link-state completion requires links wholly within the declared area"
                        )
            key = "--".join(sorted((a.local_node, a.remote_node)))
            links[key] = dict(
                id=key,
                source=min(a.local_node, a.remote_node),
                target=max(a.local_node, a.remote_node),
            )
            interfaces[key, a.local_node].add(interface)
        if not physical or any(
            (b, a, remote, local) not in physical for a, b, local, remote in physical
        ):
            raise ValueError(
                "Link-state synthesis requires both directions of every declared link"
            )
        if completion.parallel_links == "reject" and any(
            len(values) != 1 for values in interfaces.values()
        ):
            raise ValueError(
                "Parallel Link-state links require an explicit equal-cost sketch"
            )
        payload = dict(
            kind="ospf_area",
            routers=[],
            links=[],
            paths=[],
            ecmp_paths=[],
            cost_groups=[],
        )
        groups = defaultdict(list)
        for index, (state, contract) in enumerate(sorted(states.items())):
            # Disjoint state graphs share cost variables through explicit sketch equalities.
            names = {node: f"s{index}_{node}" for node in completion.routers}
            payload["routers"].extend(names.values())
            state_physical = {
                (a.local_node, a.remote_node, a.local_address, a.remote_address)
                for i in contract.intents
                if (a := i.adjacency) is not None
                and a.protocol == protocol
                and a.local_node in names
                and a.remote_node in names
            }
            if not state_physical <= physical or any(
                (b, a, remote, local) not in state_physical
                for a, b, local, remote in state_physical
            ):
                raise ValueError(
                    "Link-state states may only remove declared bidirectional links"
                )
            edges = {tuple(sorted((a, b))) for a, b, _, _ in state_physical}
            for key, link in sorted(links.items()):
                if (link["source"], link["target"]) not in edges:
                    continue
                state_id = f"{state}:{key}"
                payload["links"].append(
                    dict(
                        id=state_id,
                        source=names[link["source"]],
                        target=names[link["target"]],
                    )
                )
                groups[key].append(state_id)
            for r in selected:
                paths = (
                    (r.path_in(state),)
                    if isinstance(r, OspfPathRequirement)
                    else r.paths_in(state)
                )
                by_endpoints = defaultdict(list)
                for path in paths:
                    if any(
                        tuple(sorted((a, b))) not in edges
                        for a, b in zip(path, path[1:])
                    ):
                        raise ValueError("Link-state path traverses an undeclared link")
                    by_endpoints[path[0], path[-1]].append(
                        [names[node] for node in path]
                    )
                for paths in by_endpoints.values():
                    if len(paths) == 1:
                        payload["paths"].append(paths[0])
                    else:
                        payload["ecmp_paths"].append(paths)
        payload["cost_groups"] = list(groups.values())
        result = run_netcomplete(
            payload, directory, image=image, timeout=spec.solver_timeout_sec
        )
        if set(result) != {link["id"] for link in payload["links"]} or any(
            len({result[name] for name in group}) != 1 for group in groups.values()
        ):
            raise ValueError("Incomplete or inconsistent Link-state cost solution")
        values = tuple(
            (
                IsisMetric(
                    node=node, interface=interface, metric=result[groups[key][0]]
                )
                if isis
                else OspfCost(
                    node=node, interface=interface, cost=result[groups[key][0]]
                )
            )
            for (key, node), interfaces_for_node in sorted(interfaces.items())
            for interface in sorted(interfaces_for_node)
        )
        return RoutingParameters(**{"isis_metrics" if isis else "ospf_costs": values})
    if isinstance(completion, BgpImportCompletion):
        by_id = {r.id: r for r in spec.behavior}
        selected = [by_id.get(name) for name in completion.requirements]
        if len(set(completion.requirements)) != len(completion.requirements) or any(
            not isinstance(r, BgpSelectionRequirement) for r in selected
        ):
            raise ValueError(
                "BGP completion must reference distinct original selection requirements"
            )
        peers = env.bgp_import_peers()
        return _complete_imports(
            selected, peers, directory, image, spec.solver_timeout_sec
        )
    raise ValueError("No supported routing completion was declared")


def _complete_imports(selected, peers, directory, image, timeout):
    if not peers:
        raise ValueError("BGP import completion requires a declared session sketch")
    by_node = defaultdict(list)
    for requirement in selected:
        by_node[requirement.node].append(requirement)
    imports = []
    for index, (node, requirements) in enumerate(sorted(by_node.items())):
        neighbors = [p for p in peers if p.node == node]
        if not neighbors or len({p.local_asn for p in neighbors}) != 1:
            raise ValueError("BGP completion requires one local ASN per node")
        prefixes = [r.prefix for r in requirements]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("BGP selection must be unique per node and prefix")
        if any(
            not set(r.candidates) <= {p.interface for p in neighbors}
            for r in requirements
        ):
            raise ValueError("BGP selection references an unknown sketch neighbor")
        payload = dict(
            kind="bgp_import_order",
            asn=neighbors[0].local_asn,
            local_prefixes=[],
            peers=[
                dict(
                    name=p.interface,
                    asn=p.remote_asn,
                    relationship="provider",
                    prefixes=prefixes,
                )
                for p in neighbors
            ],
            exit_preferences=[
                dict(prefix=r.prefix, peers=r.candidates) for r in requirements
            ],
        )
        result = run_netcomplete(
            payload,
            directory / f"node-{index}",
            image=image,
            timeout=timeout,
        )
        expected = {(p.interface, prefix) for p in neighbors for prefix in prefixes}
        actual = [
            (policy["peer"], term["prefix"])
            for policy in result
            for term in policy["import_terms"]
        ]
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("Incomplete prefix-specific BGP import solution")
        terms = {
            (policy["peer"], term["prefix"]): term
            for policy in result
            for term in policy["import_terms"]
        }
        for requirement in requirements:
            if any(
                terms[p.interface, requirement.prefix]["permit"]
                != (p.interface in requirement.candidates)
                for p in neighbors
            ):
                raise ValueError(
                    "Solved BGP imports differ from the original admissible neighbors"
                )
            preferences = [
                terms[c, requirement.prefix]["local_preference"]
                for c in requirement.candidates
            ]
            if any(
                a is None or b is None or a <= b
                for a, b in zip(preferences, preferences[1:])
            ):
                raise ValueError("Solved BGP preferences violate the original ordering")
        for policy in result:
            for term in policy["import_terms"]:
                imports.append(
                    ImportPreference(
                        node=node,
                        interface=policy["peer"],
                        prefix=term["prefix"],
                        permit=term["permit"],
                        local_preference=term["local_preference"],
                    )
                )
    return RoutingParameters(import_preferences=tuple(imports))
