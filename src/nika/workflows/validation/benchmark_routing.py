"""Compile benchmark-selected ISP profiles into the shared routing workflow.

Original topology, protocol and prefix ownership define link-state paths. The shared
NetComplete workflow completes link metrics and supported AS-local import policies.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from copy import deepcopy
from textwrap import indent

import yaml
from nika.net_env.isp.igp.ifaces import srl_e1_name
from ipaddress import IPv4Network


from nika.net_env.contract import (
    AdjacencyExpectation,
    BgpRouteExpectation,
    NetworkEntity,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
)
from nika.net_env.synthesis.completion import (
    OspfAreaCompletion,
    IsisLevelCompletion,
    BgpAsImportCompletion,
)
from nika.net_env.synthesis.requirements import (
    OspfPathsRequirement,
    IsisPathsRequirement,
    link_state_paths_intents,
    BgpAsSelectionRequirement,
    default_as_selections,
)
from nika.net_env.isp.contract import bgp_adjacency_intents
from nika.net_env.isp.identity import is_isp_scenario
from nika.net_env.utils.kathara.frr_origin import BGP_ORIGIN_IFACE
from nika.net_env.isp.igp.config import DEFAULT_IGP
from nika.net_env.isp.profiles import default_device_profile
from nika.net_env.isp.igp.plan import active_igp_links, igp_components, shortest_paths
from nika.net_env.net_env_pool import list_all_net_envs, resolve_scenario_backend
from nika.problems.rca.inventory import load_offline_net_env
from nika.workflows.benchmark.load_config import load_benchmark_input
from nika.workflows.validation.routing_spec import (
    RoutingDeployment,
    RoutingExperiment,
    RoutingIncident,
    RoutingFailureCheck,
    RoutingMutation,
    RoutingRuntimeMutation,
)


def _profile_limitations(deployment, features):
    reasons = []
    if (deployment["backend"], deployment["device_profile"]) not in {
        ("kathara", "frr"),
        ("kathara", "iosxr"),
        ("kathara", "routeros"),
        ("containerlab", "nokia_srlinux"),
    }:
        reasons.append(
            "Native ISP vendor observation and configuration analysis remain required"
        )
    if deployment["device_profile"] in {"iosxr", "routeros"} and (
        deployment["igp"] != "ospf"
        or deployment["bgp_mode"] not in {"none", "ibgp_rr", "ebgp"}
    ):
        reasons.append(
            "Native ISP vendor profiles require IPv4 OSPF and supported BGP modes"
        )
    if features and (
        not set(features) <= {"rpki", "rtbh"}
        or deployment["device_profile"] != "frr"
        or deployment["backend"] != "kathara"
    ):
        reasons.append("Policy semantics require separate coverage")
    return reasons


def _profile_key(scenario, deployment):
    parts = [scenario, deployment["igp"], deployment["bgp_mode"], deployment["backend"]]
    # Preserve existing default-profile keys and distinguish native variants.
    if deployment["device_profile"] != default_device_profile(deployment["backend"]):
        parts.append(deployment["device_profile"])
    return "--".join(parts)


def isp_catalog_profiles():
    """Inventory actual registered defaults, independently of benchmark selection."""
    from nika.net_env.isp.identity import is_isp_named_special

    profiles = {}
    for scenario, registration in sorted(list_all_net_envs().items()):
        if not is_isp_scenario(scenario):
            continue
        defaults = dict(registration.deploy_defaults or {})
        backend = resolve_scenario_backend(
            scenario, backend=defaults.get("backend"), default_when_ambiguous="kathara"
        )
        deployment = dict(
            igp=defaults.get("igp", DEFAULT_IGP),
            bgp_mode=defaults.get("bgp_mode", "none"),
            backend=backend,
            device_profile=defaults.get(
                "device_profile", default_device_profile(backend)
            ),
            rpki=False,
        )
        features = set()
        if is_isp_named_special(scenario):
            fixed = load_offline_net_env(
                scenario, backend=backend, device_profile=deployment["device_profile"]
            )
            deployment.update(igp=fixed.igp, bgp_mode=fixed.bgp_mode, rpki=fixed.rpki)
            if fixed.rpki:
                features.add("rpki")
            if fixed.rtbh:
                features.add("rtbh")
        limitations = _profile_limitations(deployment, features)
        key = _profile_key(scenario, deployment)
        profiles[key] = dict(
            scenario=scenario,
            deployment=deployment,
            features=sorted(features),
            supported=not limitations,
            limitations=limitations,
            benchmark_cases=[],
            source="registered-default",
        )
    return profiles


def benchmark_isp_profiles(manifests: list[Path]):
    """Deduplicate actual deployment profiles; keep their benchmark provenance."""
    registered = list_all_net_envs()
    profiles = {}
    fixed_profiles = {}
    for manifest in manifests:
        for row in load_benchmark_input(manifest):
            scenario = row["scenario"]
            if not is_isp_scenario(scenario):
                continue
            defaults = dict(registered[scenario].deploy_defaults or {})
            deployment = {
                key: row.get(key, defaults.get(key))
                for key in ("igp", "bgp_mode", "backend", "device_profile", "rpki")
            }
            deployment["igp"] = deployment["igp"] or DEFAULT_IGP
            deployment["bgp_mode"] = deployment["bgp_mode"] or "none"
            deployment["backend"] = resolve_scenario_backend(
                scenario,
                backend=deployment["backend"],
                default_when_ambiguous="kathara",
            )
            deployment["device_profile"] = deployment[
                "device_profile"
            ] or default_device_profile(deployment["backend"])
            deployment["rpki"] = bool(deployment["rpki"])
            features = set()
            if row.get("igp") is None or row.get("bgp_mode") is None:
                # Fixed scenario constructors can bake a protocol overlay that
                # is absent from the row and registry deployment defaults.
                binding = scenario, deployment["backend"], deployment["device_profile"]
                if binding not in fixed_profiles:
                    fixed_profiles[binding] = load_offline_net_env(
                        scenario,
                        backend=deployment["backend"],
                        device_profile=deployment["device_profile"],
                    )
                fixed = fixed_profiles[binding]
                deployment.update(
                    igp=fixed.igp, bgp_mode=fixed.bgp_mode, rpki=fixed.rpki
                )
                if getattr(fixed, "rtbh", False):
                    features.add("rtbh")
            if deployment["rpki"]:
                features.add("rpki")
            key = _profile_key(scenario, deployment)
            if key not in profiles:
                reasons = _profile_limitations(deployment, features)
                profiles[key] = {
                    "scenario": scenario,
                    "deployment": deployment,
                    "features": sorted(features),
                    "supported": not reasons,
                    "limitations": reasons,
                    "benchmark_cases": [],
                }
            elif profiles[key]["deployment"] != deployment:
                raise ValueError(
                    f"Conflicting device or policy variants for benchmark profile {key}"
                )
            provenance = {
                "manifest": str(manifest),
                "problem": row["problem"],
                "inject": row["inject"],
            }
            if provenance not in profiles[key]["benchmark_cases"]:
                profiles[key]["benchmark_cases"].append(provenance)
    return dict(sorted(profiles.items()))


def build_benchmark_isp_experiment(
    profile, *, bgp_selections: tuple[BgpAsSelectionRequirement, ...] | None = None
) -> RoutingExperiment:
    """Resolve healthy and link-loss expectations from the fixed original graph."""
    if not profile["supported"]:
        raise ValueError(
            "Unsupported benchmark profile: " + "; ".join(profile["limitations"])
        )
    scenario = profile["scenario"]
    deployment = RoutingDeployment.model_validate(profile["deployment"])
    vendor = deployment.device_profile in {"iosxr", "routeros"}
    if vendor:
        limitations = _profile_limitations(
            deployment.model_dump(), profile.get("features", ())
        )
        if limitations:
            raise ValueError("Unsupported benchmark profile: " + "; ".join(limitations))
        unsupported_faults = {
            case["problem"]
            for case in profile["benchmark_cases"]
            if case["problem"] != "link_down"
        }
        if unsupported_faults:
            raise ValueError(
                "Native ISP experiment needs native fault bindings for: "
                + ", ".join(sorted(unsupported_faults))
            )
    env = load_offline_net_env(scenario, **deployment.model_dump(exclude_none=True))
    plan = env.plan
    srl = env.device_profile == "nokia_srlinux"
    if bgp_selections is None:
        bgp_selections = default_as_selections(env.bgp_plan)
    if bgp_selections and env.bgp_mode != "ebgp":
        raise ValueError("AS import completion requires a fixed eBGP sketch")
    from nika.net_env.isp.bgp.policy import fixed_policy_prefixes

    if bgp_selections and any(
        r.prefix in fixed_policy_prefixes(env.bgp_plan) for r in bgp_selections
    ):
        raise ValueError(
            "AS import completion cannot replace fixed security prefix policies"
        )
    if (
        plan.igp not in {"ospf", "isis"}
        or (env.backend != "kathara" and not srl)
        or env.bgp_mode not in {"none", "ebgp", "ibgp_rr"}
    ):
        raise ValueError(
            "Benchmark routing adapter requires a supported ISP protocol/backend combination"
        )
    links = sorted(active_igp_links(plan), key=lambda link: link.link_id)
    nodes = {n.device_name: n for n in plan.nodes}
    anchor = min(nodes)
    components = igp_components(plan)
    anchor_for = {node: component[0] for component in components for node in component}
    component_for = {
        node: set(component) for component in components for node in component
    }
    # Prefer a declared benchmark outage attachment over an arbitrary link.
    declared_down = next(
        (c for c in profile["benchmark_cases"] if c["problem"] == "link_down"), None
    )
    if declared_down:
        target = declared_down["inject"]
        matching = [
            edge
            for edge in links
            if (
                target["host_name"],
                ("eth" + str(int(target["intf_name"][3:]) - 1))
                if srl
                else target["intf_name"],
            )
            in {(edge.endpoint_a, edge.iface_a), (edge.endpoint_b, edge.iface_b)}
        ]
        if len(matching) != 1:
            raise ValueError(
                "Benchmark link-down target must resolve to one active IGP link"
            )
        links.remove(matching[0])
        links.insert(0, matching[0])
    if env.bgp_plan:
        # Keep iBGP transport connected in this declared link experiment. BGP
        # session failures are exercised separately through registered failures.
        link = next(
            (
                edge
                for edge in (links[:1] if declared_down else links)
                if igp_components(
                    replace(
                        plan,
                        links=tuple(
                            other
                            for other in plan.links
                            if other.link_id != edge.link_id
                        ),
                    )
                )
                == components
            ),
            None,
        )
        if link is None:
            raise ValueError(
                "BGP overlay experiment requires a redundant intra-AS IGP link"
            )
        links.remove(link)
        links.insert(0, link)
    origin = {
        "basis": (
            "RFC 1195 single-level IS-IS SPF and RFC 5305 wide metrics"
            if plan.igp == "isis"
            else "RFC 2328 OSPF adjacency and graph reachability"
        )
        + "; existing benchmark protocol profile and prefix ownership",
        "benchmark_cases": profile["benchmark_cases"],
        "scope": f"Directed physical {plan.igp.upper()} adjacencies, bidirectional anchor-to-router loopback connectivity and exact ECMP paths in healthy and declared failure states; no all-pairs claim",
        "synthesis": f"NetComplete completes active {plan.igp.upper()} interface metrics; AS boundaries, RR roles and existing BGP policies remain fixed sketch inputs",
    }

    def contract(removed=()):
        remaining = tuple(link for link in plan.links if link.link_id not in removed)
        components = igp_components(replace(plan, links=remaining))
        component = {n: index for index, c in enumerate(components) for n in c}
        intents = []
        for link in links:
            if link.link_id in removed:
                continue
            for left, right, local, remote in [
                (link.endpoint_a, link.endpoint_b, link.address_a, link.address_b),
                (link.endpoint_b, link.endpoint_a, link.address_b, link.address_a),
            ]:
                intents.append(
                    ValidationIntent(
                        id=f"adj.{left}.{right}.{local.replace('.', '-')}",
                        description="The declared OSPF adjacency must be Full."
                        if plan.igp == "ospf"
                        else "The declared Level-2 IS-IS adjacency must be Up.",
                        property="adjacency",
                        expected="established",
                        adjacency=AdjacencyExpectation(
                            protocol=plan.igp,
                            isis_level=2 if plan.igp == "isis" else None,
                            local_node=left,
                            remote_node=right,
                            local_address=local,
                            remote_address=remote,
                            ospf_area="0.0.0.0" if plan.igp == "ospf" else None,
                            local_interface=(
                                link.iface_a
                                if left == link.endpoint_a
                                else link.iface_b
                            )
                            if plan.igp == "isis"
                            else None,
                            remote_interface=(
                                link.iface_b
                                if left == link.endpoint_a
                                else link.iface_a
                            )
                            if plan.igp == "isis"
                            else None,
                            local_router_id=nodes[left].router_id,
                            remote_router_id=nodes[right].router_id,
                        ),
                    )
                )
        pairs = {(anchor, other) for other in set(nodes) - {anchor}}
        pairs.update(
            (anchor_for[node], node) for node in nodes if anchor_for[node] != node
        )
        for left, right in sorted(pairs):
            connected = component[left] == component[right]
            for source, target in [(left, right), (right, left)]:
                intents.append(
                    ValidationIntent(
                        id=f"reach.{source}.{target}",
                        description="Original loopback connectivity follows the declared surviving topology.",
                        property="reachability" if connected else "isolation",
                        expected="reachable" if connected else "unreachable",
                        source=NetworkEntity(
                            kind="node", name=source, address=nodes[source].loopback
                        ),
                        destination=NetworkEntity(
                            kind="node", name=target, address=nodes[target].loopback
                        ),
                        traffic=TrafficSelector(protocol="icmp", round_trip=True),
                    )
                )
        if env.bgp_plan:
            intents.extend(bgp_adjacency_intents(env.bgp_plan, verify_rr_roles=True))
            excluded = (
                set(env.bgp_plan.inventory.get("leak_prefixes", ()))
                if env.rpki
                else set()
            )
            origins = [o for o in env.bgp_plan.originated if o.prefix not in excluded]
            for source in origins:
                for target in origins:
                    if source == target:
                        continue
                    intents.append(
                        ValidationIntent(
                            id=f"business.{source.device}.{target.device}",
                            description="Originally announced business endpoints must communicate through the original BGP overlay.",
                            property="reachability",
                            expected="reachable",
                            source=NetworkEntity(
                                kind="node",
                                name=source.device,
                                address=source.ping_address,
                            ),
                            destination=NetworkEntity(
                                kind="node",
                                name=target.device,
                                address=target.ping_address,
                            ),
                            traffic=TrafficSelector(protocol="icmp", round_trip=True),
                        )
                    )
        if env.bgp_plan and (env.rpki or getattr(env, "rtbh", False)):
            info = env.bgp_plan.inventory

            def policy_flow(identifier, node, return_asn, prefix, reachable):
                (origin,) = [o for o in env.bgp_plan.originated if o.prefix == prefix]
                # An addressed inter-AS attachment supplies a return path through
                # that AS's original passive IGP subnet announcement.
                address = min(
                    s.local_ip
                    for s in env.bgp_plan.sessions
                    if s.local_device == node
                    and s.remote_asn == return_asn
                    and s.session_type == "ebgp"
                )
                return ValidationIntent(
                    id=identifier,
                    description="Original policy-defined forwarding behavior.",
                    property="reachability" if reachable else "isolation",
                    expected="reachable" if reachable else "unreachable",
                    source=NetworkEntity(kind="node", name=node, address=address),
                    destination=NetworkEntity(
                        kind="node", name=origin.device, address=origin.ping_address
                    ),
                    traffic=TrafficSelector(protocol="icmp", round_trip=True),
                )

            if env.rpki:
                for role in ("rov_observer", "non_rov_observer"):
                    for prefix in info["leak_prefixes"]:
                        intents.append(
                            ValidationIntent(
                                id=f"rpki.{role}.{prefix.replace('/', '-')}",
                                description="The original dormant unauthorized origin must have no selected BGP route.",
                                property="bgp_route",
                                expected="absent",
                                bgp_route=BgpRouteExpectation(
                                    node=info[role], prefix=prefix, present=False
                                ),
                            )
                        )
            if getattr(env, "rtbh", False):
                prefix = info["target_prefix"]
                intents.append(
                    policy_flow(
                        "rtbh.provider." + prefix.replace("/", "-"),
                        info["rtbh_provider_device"],
                        info["legitimate_origin_asn"],
                        prefix,
                        True,
                    )
                )
        unauthorized = set()
        for case in profile["benchmark_cases"]:
            if case["problem"] != "bgp_hijacking":
                continue
            prefix = str(IPv4Network(case["inject"]["target_network"]))
            if any(o.prefix == prefix for o in env.bgp_plan.originated):
                raise ValueError(
                    "Unauthorized-origin control requires an unowned prefix"
                )
            observer = case["inject"]["symptom_host"]
            observers = {observer}
            if observer == case["inject"]["host_name"]:
                # A local connected route alone cannot demonstrate propagation.
                observers.add(
                    min(
                        o.device
                        for o in env.bgp_plan.originated
                        if o.device != observer
                    )
                )
            for observer in sorted(observers):
                if (observer, prefix) in unauthorized:
                    continue
                unauthorized.add((observer, prefix))
                intents.append(
                    ValidationIntent(
                        id=f"isolate.unauthorized.{observer}.{prefix.replace('/', '-')}",
                        description="The original unowned prefix must remain unreachable.",
                        property="isolation",
                        expected="unreachable",
                        source=NetworkEntity(
                            kind="node", name=observer, address=nodes[observer].loopback
                        ),
                        destination=NetworkEntity(
                            kind="prefix", name=prefix, address=prefix
                        ),
                        traffic=TrafficSelector(protocol="icmp", round_trip=True),
                    )
                )
        return ValidationContract(
            contract_id=f"benchmark.{scenario}.{plan.igp}",
            scenario=scenario,
            design_source=origin,
            intents=tuple(sorted(intents, key=lambda i: i.id)),
        )

    sources_by_destination = {}

    def paths_to(destination, removed=()):
        graph = {node: [] for node in nodes}
        pairs = {}
        for edge in links:
            if edge.link_id in removed:
                continue
            a, b = edge.endpoint_a, edge.endpoint_b
            key = tuple(sorted((a, b)))
            if key in pairs and pairs[key] != edge.metric:
                raise ValueError(
                    "Equal-cost parallel synthesis requires equal original metrics"
                )
            pairs[key] = edge.metric
        for (a, b), metric in pairs.items():
            graph[a].append((b, metric))
            graph[b].append((a, metric))
        sources = sources_by_destination.get(
            destination,
            sorted(component_for[destination] - {destination})
            if destination == anchor_for[destination]
            else [anchor_for[destination]],
        )
        if env.bgp_plan and any(
            case["problem"] == "bgp_acl_block" for case in profile["benchmark_cases"]
        ):
            # A transit TCP ACL depends on session transport, not just peer
            # endpoints. Preserve those original paths before solving metrics.
            sources = sorted(
                set(sources)
                | {
                    session.local_device
                    for session in env.bgp_plan.sessions
                    if session.session_type == "ibgp"
                    and session.remote_device == destination
                }
            )
        return tuple(
            path
            for source in sources
            for path in shortest_paths(graph, source, destination)
        )

    original_paths = {
        destination: paths_to(destination) for destination in sorted(nodes)
    }
    # Every generated per-hop predicate is an independent destination-route
    # requirement, even if the original anchor becomes isolated in a fault.
    sources_by_destination = {
        destination: sorted({node for path in paths for node in path[:-1]})
        for destination, paths in original_paths.items()
    }
    behavior = tuple(
        (IsisPathsRequirement if plan.igp == "isis" else OspfPathsRequirement)(
            kind="isis_paths" if plan.igp == "isis" else "ospf_paths",
            id=f"path.{destination}",
            **({"level": 2} if plan.igp == "isis" else {"area": 0}),
            prefix=nodes[destination].loopback + "/32",
            paths=original_paths[destination],
            state_paths={
                "backbone-link-down": paths_to(destination, {links[0].link_id})
            },
        )
        for destination in sorted(nodes)
        if original_paths[destination]
    )

    def with_paths(original, removed=()):
        adjacencies = [i.adjacency for i in original.intents if i.adjacency]
        generated = [
            intent
            for r in behavior
            for intent in link_state_paths_intents(
                r, paths_to(r.paths[0][-1], removed), adjacencies
            )
        ]
        return ValidationContract.model_validate(
            {
                **original.model_dump(),
                "intents": sorted([*original.intents, *generated], key=lambda i: i.id),
            }
        )

    healthy = contract()
    link = links[0]
    fault_checks = []
    for case in profile["benchmark_cases"]:
        if case["problem"] in {
            "bgp_rpki_invalid_route_leak",
            "bgp_blackhole_community_leak",
        }:
            if any(
                f.problem == case["problem"] and f.params == case["inject"]
                for f in fault_checks
            ):
                continue
            info = env.bgp_plan.inventory
            if case["problem"] == "bgp_rpki_invalid_route_leak":
                failed = tuple(
                    i.id
                    for i in healthy.intents
                    if i.id.startswith("rpki.non_rov_observer.")
                )
                edits = {
                    node: ("route-map BGP-OUT deny 5", "route-map BGP-OUT permit 5")
                    for node in info["leaker_as_devices"]
                }
                restore_nodes = tuple(info["leaker_as_devices"])
                positive_intents = tuple(
                    i.model_copy(
                        update={
                            "expected": "selected",
                            "bgp_route": i.bgp_route.model_copy(
                                update={
                                    "present": True,
                                    "origin_asn": info["leaker_asn"],
                                }
                            ),
                        }
                    )
                    if i.id in failed
                    else i
                    for i in healthy.intents
                )
                name = "benchmark-rpki-leak"
            else:
                from nika.problems.routing_control_plane.bgp import _RTBH_PREFIX_LIST

                route_map, prefix, community = (
                    info["leaker_outbound_route_map"],
                    info["target_prefix"],
                    info["blackhole_community"],
                )
                old = f"route-map {route_map} permit 10"
                new = (
                    f"ip prefix-list {_RTBH_PREFIX_LIST} seq 5 permit {prefix} le 24\nroute-map {route_map} permit 5\n match ip address prefix-list {_RTBH_PREFIX_LIST}\n set community {community} additive\n!\n"
                    + old
                )
                edits = {info["leaker_device"]: (old, new)}
                failed = tuple(
                    i.id for i in healthy.intents if i.id.startswith("rtbh.provider.")
                )
                restore_nodes = (info["leaker_device"],)
                positive_intents = tuple(
                    i.model_copy(
                        update={"property": "isolation", "expected": "unreachable"}
                    )
                    if i.id in failed
                    else i
                    for i in healthy.intents
                    if not i.id.startswith("business.")
                )
                name = "benchmark-rtbh-community"
            fault_checks.append(
                RoutingFailureCheck(
                    name=name,
                    problem=case["problem"],
                    params=case["inject"],
                    restore_nodes=restore_nodes,
                    expected_failed_ids=failed,
                    requirements=healthy.model_copy(
                        update={"intents": positive_intents}
                    ),
                    static_edits=edits,
                )
            )
            continue
        if env.bgp_mode == "ibgp_rr" and case["problem"] in {
            "bgp_asn_misconfig",
            "bgp_acl_block",
            "bgp_missing_route_advertisement",
            "bgp_hijacking",
        }:
            params, problem = case["inject"], case["problem"]
            target = params["host_name"]
            name = "benchmark-" + problem.replace("_", "-")
            same_problem = [f for f in fault_checks if f.problem == problem]
            if any(f.params == params for f in same_problem):
                continue
            if same_problem:
                name = f"benchmark-{problem.split('_')[1]}-{len(same_problem) + 1}"
            recovery_commands = {}
            nft_nodes = ()
            static_edits = {}
            if problem == "bgp_asn_misconfig":
                asn = next(n.asn for n in env.bgp_plan.nodes if n.device_name == target)
                # The registered failure changes the local ASN by +600.
                old = f"autonomous-system: {asn}\n" if srl else f"router bgp {asn}\n"
                new = (
                    f"autonomous-system: {asn + 600}\n"
                    if srl
                    else f"router bgp {asn + 600}\n"
                )
                static_edits = {target: (old, new)}
            if problem in {"bgp_asn_misconfig", "bgp_acl_block"}:
                failed = tuple(
                    sorted(
                        i.id
                        for i in healthy.intents
                        if (
                            i.adjacency
                            and i.adjacency.protocol == "bgp"
                            and target
                            in (i.adjacency.local_node, i.adjacency.remote_node)
                        )
                        or (
                            i.id.startswith("business.")
                            and target in (i.source.name, i.destination.name)
                        )
                    )
                )
                if problem == "bgp_acl_block" and not srl:
                    nft_nodes = (target,)
                    blocked = set()
                    for intent in healthy.intents:
                        adjacency = intent.adjacency
                        if not adjacency or adjacency.protocol != "bgp":
                            continue
                        paths = [
                            path
                            for path in original_paths[adjacency.remote_node]
                            if path[0] == adjacency.local_node
                        ]
                        if not paths:
                            raise ValueError(
                                "BGP ACL requires declared session transport paths"
                            )
                        hits = [target in path for path in paths]
                        if any(hits) != all(hits):
                            raise ValueError(
                                "BGP ACL with mixed filtered/unfiltered ECMP paths requires flow-hash semantics"
                            )
                        if all(hits):
                            blocked.add(intent.id)
                    failed = tuple(
                        sorted(
                            blocked | {i for i in failed if not i.startswith("adj.")}
                        )
                    )
            elif problem == "bgp_missing_route_advertisement":
                if not any(
                    o.device == target and o.prefix == params["prefix"]
                    for o in env.bgp_plan.originated
                ):
                    raise ValueError(
                        "Benchmark withdrawal must name an original owned prefix"
                    )
                failed = tuple(
                    sorted(
                        i.id
                        for i in healthy.intents
                        if i.id.startswith("business.")
                        and target in (i.source.name, i.destination.name)
                    )
                )
            else:
                prefix = str(IPv4Network(params["target_network"]))
                failed = tuple(
                    sorted(
                        i.id
                        for i in healthy.intents
                        if i.id.startswith("isolate.unauthorized.")
                        and i.destination.address == prefix
                    )
                )
                recovery_commands = {
                    target: (
                        f"vtysh -c 'configure terminal' -c 'interface {BGP_ORIGIN_IFACE}' -c 'no ip address {prefix}' -c 'end'",
                        f"ip route del local {prefix} dev {BGP_ORIGIN_IFACE} table local 2>/dev/null || true",
                        f"ip addr del {prefix} dev {BGP_ORIGIN_IFACE} 2>/dev/null || true",
                    )
                }
            positive = healthy.model_copy(
                update={
                    "intents": tuple(i for i in healthy.intents if i.id not in failed)
                }
            )
            if problem == "bgp_hijacking":
                positive = healthy.model_copy(
                    update={
                        "intents": tuple(
                            i.model_copy(
                                update={
                                    "property": "reachability",
                                    "expected": "reachable",
                                }
                            )
                            if i.id in failed
                            else i
                            for i in healthy.intents
                        )
                    }
                )
            fault_checks.append(
                RoutingFailureCheck(
                    name=name,
                    problem=problem,
                    params=params,
                    restore_nodes=(target,),
                    requirements=positive,
                    expected_failed_ids=failed,
                    recovery_commands=recovery_commands,
                    restore_nft_nodes=nft_nodes,
                    static_edits=static_edits,
                )
            )
            continue
        if case["problem"] == "bgp_max_prefix_exceeded":
            receiver, peer = (
                case["inject"]["receiver_name"],
                case["inject"]["peer_name"],
            )
            session = next(
                s
                for s in env.bgp_plan.sessions
                if s.local_device == receiver
                and s.remote_device == peer
                and s.remote_ip == case["inject"]["neighbor_ip"]
            )
            paired = [
                i
                for i in healthy.intents
                if i.adjacency
                and i.adjacency.protocol == "bgp"
                and (
                    (
                        i.adjacency.local_node,
                        i.adjacency.remote_node,
                        i.adjacency.remote_address,
                    )
                    == (receiver, peer, session.remote_ip)
                    or (
                        i.adjacency.local_node,
                        i.adjacency.remote_node,
                        i.adjacency.local_address,
                    )
                    == (peer, receiver, session.remote_ip)
                )
            ]
            name = f"benchmark-max-prefix-{receiver}"
            if not any(f.name == name for f in fault_checks):
                fault_checks.append(
                    RoutingFailureCheck(
                        name=name,
                        problem=case["problem"],
                        params=case["inject"],
                        restore_nodes=(receiver, peer),
                        expected_failed_ids=tuple(sorted(i.id for i in paired)),
                    )
                )
            continue
        if case["problem"] != "ospf_area_misconfiguration":
            continue
        target = case["inject"]["host_name"]
        removed = {
            link.link_id
            for link in links
            if target in (link.endpoint_a, link.endpoint_b)
        }
        affected = with_paths(contract(removed), removed)
        by_id = {i.id: i for i in affected.intents}
        failed = sorted(
            i.id
            for i in with_paths(healthy).intents
            if i.id not in by_id
            or by_id[i.id].property == "isolation"
            or i.route_next_hops != by_id[i.id].route_next_hops
        )
        name = "benchmark-area-" + target
        from nika.problems.routing_control_plane.ospf import _ospf_networks

        config = env.lab.machines[target].fs.readtext("etc/frr/frr.conf")
        old = (
            "router ospf\n"
            + config.split("router ospf\n", 1)[1].split("\n!", 1)[0]
            + "\n"
        )
        statements = _ospf_networks(old)
        correct_area = statements[0][1]
        wrong_area = "66" if correct_area != "66" else "99"
        new = old
        for prefix, area in statements:
            if area == correct_area:
                new = new.replace(
                    f"network {prefix} area {area}\n",
                    f"network {prefix} area {wrong_area}\n",
                )
        behavior = tuple(
            r.model_copy(
                update={
                    "state_paths": {
                        **r.state_paths,
                        name: paths_to(r.paths[0][-1], removed),
                    }
                }
            )
            for r in behavior
        )
        if not any(f.name == name for f in fault_checks):
            fault_checks.append(
                RoutingFailureCheck(
                    name=name,
                    problem=case["problem"],
                    params=case["inject"],
                    restore_nodes=(target,),
                    requirements=contract(removed),
                    expected_failed_ids=tuple(failed),
                    static_edits={target: (old, new)},
                )
            )
    # Removing one original loopback announcement isolates the return path while
    # preserving link sessions. The expected failures come from prefix ownership.
    withdrawal_anchor = min(node for node in nodes if len(component_for[node]) > 1)
    target = min(component_for[withdrawal_anchor] - {withdrawal_anchor})
    prefix = nodes[target].loopback + "/32"
    destination_id = f"reach.{withdrawal_anchor}.{target}"
    withdrawn_ids = tuple(
        i.id
        for i in with_paths(healthy).intents
        if i.route_next_hops and i.route_next_hops.prefix == prefix
    )
    static_name, runtime_name = "missing-loopback-announcement", "loopback-withdrawal"
    announcement = f"network {prefix} area 0.0.0.0"
    old_announcement = " " + announcement
    context = ("router ospf",)
    static_failed = tuple(sorted((destination_id, *withdrawn_ids)))
    runtime_failed = tuple(
        sorted((destination_id, f"reach.{target}.{withdrawal_anchor}", *withdrawn_ids))
    )
    if env.bgp_plan:
        # Withdrawing an iBGP transport loopback also destroys the sessions.
        # Exercise business-prefix provenance independently of that transport.
        originated = env.bgp_plan.originated[0]
        target, prefix = originated.device, originated.prefix
        static_name, runtime_name = (
            "missing-business-announcement",
            "business-withdrawal",
        )
        announcement = f"network {prefix}"
        old_announcement = "  " + announcement
        asn = next(
            node.asn for node in env.bgp_plan.nodes if node.device_name == target
        )
        context = (f"router bgp {asn}", "address-family ipv4 unicast")

        def uses_origin(entity):
            return (
                entity is not None
                and entity.address is not None
                and IPv4Network(entity.address, strict=False).subnet_of(
                    IPv4Network(prefix)
                )
            )

        static_failed = tuple(
            sorted(
                i.id
                for i in healthy.intents
                if i.property == "reachability"
                and i.source.name != target
                and uses_origin(i.destination)
            )
        )
        runtime_failed = tuple(
            sorted(
                i.id
                for i in healthy.intents
                if i.property == "reachability"
                and (
                    (i.source.name != target and uses_origin(i.destination))
                    or uses_origin(i.source)
                )
            )
        )
    static_replacement = ""
    withdrawal = ("no " + announcement,)
    recovery = (announcement,)
    if plan.igp == "isis" and not env.bgp_plan:
        context = ("interface lo",)
        old_announcement = (
            f"interface lo\n ip address {prefix}\n ip router isis NIKA\n isis passive"
        )
        static_replacement = f"interface lo\n ip address {prefix}"
        withdrawal = ("no ip router isis NIKA",)
        recovery = ("ip router isis NIKA", "isis passive")
    role_static, role_runtime = [], []
    if env.bgp_mode == "ibgp_rr":
        role = next(
            i
            for i in healthy.intents
            if i.adjacency and i.adjacency.route_reflector_client
        )
        adjacency = role.adjacency
        role_node, peer = adjacency.local_node, adjacency.remote_address
        asn = adjacency.local_asn
        role_static.append(
            RoutingMutation(
                name="missing-rr-client-role",
                node=role_node,
                old=f"  neighbor {peer} route-reflector-client",
                new="",
                expected_failed_ids=(role.id,),
            )
        )
        role_runtime.append(
            RoutingRuntimeMutation(
                name="rr-client-role-withdrawal",
                node=role_node,
                commands=(
                    "configure terminal",
                    f"router bgp {asn}",
                    "address-family ipv4 unicast",
                    f"no neighbor {peer} route-reflector-client",
                    "end",
                ),
                recovery=(
                    "configure terminal",
                    f"router bgp {asn}",
                    "address-family ipv4 unicast",
                    f"neighbor {peer} route-reflector-client",
                    "end",
                ),
                expected_failed_ids=(role.id,),
            )
        )
    # Both oracles evaluate echo request and reply from the same specification.
    static_failed = runtime_failed
    if srl:
        # Mutate concrete native documents, retaining the original requirement IDs.
        documents = env.routing_documents
        mutated = deepcopy(documents[target])
        if env.bgp_plan:
            policy = mutated["srl_nokia-routing-policy:routing-policy"]
            business = next(p for p in policy["prefix-set"] if p["name"] == "BUSINESS")
            prefix_row = next(p for p in business["prefix"] if p["ip-prefix"] == prefix)
            old_announcement = indent(
                yaml.safe_dump([prefix_row], sort_keys=False), " " * 4
            )
            path = f"/routing-policy prefix-set BUSINESS prefix {prefix} mask-length-range {IPv4Network(prefix).prefixlen}..{IPv4Network(prefix).prefixlen}"
            withdraw_native = ("enter candidate", f"delete {path}", "commit now")
            recover_native = (
                "enter candidate",
                path,
                "commit now",
            )
        else:
            instance = mutated["srl_nokia-network-instance:network-instance"][0]
            igp = instance["protocols"][f"srl_nokia-{plan.igp}:{plan.igp}"]["instance"][
                0
            ]
            rows = (
                igp["interface"] if plan.igp == "isis" else igp["area"][0]["interface"]
            )
            loopback_row = next(i for i in rows if i["interface-name"] == "system0.0")
            old_announcement = indent(
                yaml.safe_dump([loopback_row], sort_keys=False),
                " " * (8 if plan.igp == "isis" else 10),
            )
            path = (
                f"/network-instance default protocols {plan.igp} instance {igp['name']}"
            )
            if plan.igp == "ospf":
                path += " area " + igp["area"][0]["area-id"]
            path += " interface system0.0"
            withdraw_native = ("enter candidate", f"delete {path}", "commit now")
            recover_native = ("enter candidate", f"{path} passive true", "commit now")
        static_replacement = ""
        role_static, role_runtime = [], []
        if env.bgp_mode == "ibgp_rr":
            role = next(
                i
                for i in healthy.intents
                if i.adjacency and i.adjacency.route_reflector_client
            )
            local, peer = role.adjacency.local_node, role.adjacency.remote_address
            mutated_role = deepcopy(documents[local])
            bgp = mutated_role["srl_nokia-network-instance:network-instance"][0][
                "protocols"
            ]["srl_nokia-bgp:bgp"]
            neighbor = next(p for p in bgp["neighbor"] if p["peer-address"] == peer)
            original_neighbor = indent(
                yaml.safe_dump([neighbor], sort_keys=False), " " * 6
            )
            neighbor["route-reflector"]["client"] = False
            role_static.append(
                RoutingMutation(
                    name="missing-rr-client-role",
                    node=local,
                    old=original_neighbor,
                    new=indent(yaml.safe_dump([neighbor], sort_keys=False), " " * 6),
                    expected_failed_ids=(role.id,),
                )
            )
            path = f"/network-instance default protocols bgp neighbor {peer} route-reflector client"
            role_runtime.append(
                RoutingRuntimeMutation(
                    name="rr-client-role-withdrawal",
                    node=local,
                    commands=("enter candidate", path + " false", "commit now"),
                    recovery=("enter candidate", path + " true", "commit now"),
                    expected_failed_ids=(role.id,),
                )
            )
    elif vendor:
        if env.device_profile == "iosxr":
            if env.bgp_plan:
                withdraw_native = (
                    "configure terminal",
                    *context,
                    *withdrawal,
                    "commit",
                    "end",
                )
                recover_native = (
                    "configure terminal",
                    *context,
                    *recovery,
                    "commit",
                    "end",
                )
            else:
                old_announcement = "  interface Loopback0\n   passive enable\n  !\n"
                context = ("router ospf NIKA", "area 0")
                withdraw_native = (
                    "configure terminal",
                    *context,
                    "no interface Loopback0",
                    "commit",
                    "end",
                )
                recover_native = (
                    "configure terminal",
                    *context,
                    "interface Loopback0",
                    "passive enable",
                    "commit",
                    "end",
                )
        else:
            if env.bgp_plan:
                old_announcement = (
                    f"/ip firewall address-list add list=BGP-ORIGINS address={prefix}"
                )
                withdraw_native = (
                    "/ip firewall address-list remove "
                    f"[find where list=BGP-ORIGINS and address={prefix}]",
                )
                recover_native = (old_announcement,)
            else:
                old_announcement = next(
                    line
                    for line in env.deployment_configs[target].splitlines()
                    if line.startswith("/routing ospf interface-template add ")
                    and "interfaces=lo0 " in line
                )
                withdraw_native = (
                    "/routing ospf interface-template remove [find where interfaces=lo0]",
                )
                recover_native = (old_announcement,)
        role_static, role_runtime = [], []
        if env.bgp_mode == "ibgp_rr":
            role = next(
                i
                for i in healthy.intents
                if i.adjacency and i.adjacency.route_reflector_client
            )
            adjacency = role.adjacency
            local, peer = adjacency.local_node, adjacency.remote_address
            if env.device_profile == "iosxr":
                from nika.net_env.isp.iosxr import iosxr_interface

                session = next(
                    session
                    for session in env.bgp_plan.sessions
                    if session.local_device == local and session.remote_ip == peer
                )
                original = (
                    f" neighbor {peer}\n  remote-as {session.remote_asn}\n"
                    + (
                        f"  update-source {iosxr_interface(session.update_source)}\n"
                        if session.update_source
                        else ""
                    )
                    + "  address-family ipv4 unicast\n   route-reflector-client\n"
                )
                altered = original.replace("   route-reflector-client\n", "")
                context = (
                    f"router bgp {adjacency.local_asn}",
                    f"neighbor {peer}",
                    "address-family ipv4 unicast",
                )
                remove_role = (
                    "configure terminal",
                    *context,
                    "no route-reflector-client",
                    "commit",
                    "end",
                )
                restore_role = (
                    "configure terminal",
                    *context,
                    "route-reflector-client",
                    "commit",
                    "end",
                )
            else:
                config = env.deployment_configs[local]
                original = next(
                    line
                    for line in config.splitlines()
                    if line.startswith("/routing bgp connection add ")
                    and f"remote.address={peer} " in line
                )
                altered = original.replace("local.role=ibgp-rr ", "local.role=ibgp ")
                command = (
                    "/routing bgp connection set "
                    f"[find where remote.address={peer}] local.role="
                )
                remove_role, restore_role = (command + "ibgp",), (command + "ibgp-rr",)
            role_static.append(
                RoutingMutation(
                    name="missing-rr-client-role",
                    node=local,
                    old=original,
                    new=altered,
                    expected_failed_ids=(role.id,),
                )
            )
            role_runtime.append(
                RoutingRuntimeMutation(
                    name="rr-client-role-withdrawal",
                    node=local,
                    commands=remove_role,
                    recovery=restore_role,
                    expected_failed_ids=(role.id,),
                )
            )
    else:
        withdraw_native = ("configure terminal", *context, *withdrawal, "end")
        recover_native = ("configure terminal", *context, *recovery, "end")
    completion = (IsisLevelCompletion if plan.igp == "isis" else OspfAreaCompletion)(
        kind="isis_level" if plan.igp == "isis" else "ospf_area",
        **({"level": 2} if plan.igp == "isis" else {"area": 0}),
        routers=tuple(sorted(nodes)),
        requirements=tuple(r.id for r in behavior),
        parallel_links="equal_cost",
    )
    if bgp_selections:
        # Selection requirements precede synthesis and remain the independent
        # oracles for both concrete-config analysis and native execution.
        behavior = (*behavior, *bgp_selections)
        completion = (
            completion,
            BgpAsImportCompletion(
                kind="bgp_as_imports",
                requirements=tuple(r.id for r in bgp_selections),
            ),
        )
        withdrawn_selections = tuple(
            f"{r.id}.{n.device_name}"
            for r in bgp_selections
            if r.prefix == prefix
            for n in env.bgp_plan.nodes
            if n.asn == r.asn
        )
        static_failed = tuple(sorted((*static_failed, *withdrawn_selections)))
        runtime_failed = tuple(sorted((*runtime_failed, *withdrawn_selections)))
    return RoutingExperiment(
        scenario=scenario,
        routing_model=(
            f"{env.device_profile}-isp"
            if vendor
            else "srl-isp"
            if srl
            else "frr-isis"
            if plan.igp == "isis"
            else "frr"
        ),
        solver_timeout_sec=900,
        deployment=deployment,
        healthy=healthy,
        behavior=behavior,
        completion=completion,
        incidents=(
            RoutingIncident(
                name="backbone-link-down",
                host_name=declared_down["inject"]["host_name"]
                if declared_down
                else link.endpoint_a,
                intf_name=declared_down["inject"]["intf_name"]
                if declared_down
                else (srl_e1_name(link.iface_a) if srl else link.iface_a),
                requirements=contract({link.link_id}),
            ),
        ),
        failure_checks=tuple(fault_checks),
        counterexamples=(
            RoutingMutation(
                name=static_name,
                node=target,
                old=old_announcement,
                new=static_replacement,
                expected_failed_ids=static_failed,
            ),
            *role_static,
        ),
        runtime_counterexamples=(
            RoutingRuntimeMutation(
                name=runtime_name,
                node=target,
                commands=withdraw_native,
                recovery=recover_native,
                expected_failed_ids=runtime_failed,
            ),
            *role_runtime,
        ),
    )
