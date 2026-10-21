"""Python 2 entry point for the pinned upstream NetComplete image.

Complete OSPF costs, device-level BGP exports and abstract BGP import orders.
The caller independently
checks concrete FRR configurations and execution against original requirements.
"""

from __future__ import print_function

import json
import random
import sys

import z3
from tekton.graph import NetworkGraph
from synet.synthesis.ospf_heuristic import OSPFSyn
from synet.utils.common import ECMPPathsReq, PathReq, Protocols
from synet.utils.smt_context import VALUENOTSET


def synthesize_ospf(specification):
    graph = NetworkGraph()
    for node in specification["routers"]:
        graph.add_router(node)
        graph.enable_ospf(node, 100)
    solver = z3.Solver()
    for link in specification["links"]:
        source, destination = link["source"], link["target"]
        for left, right in [(source, destination), (destination, source)]:
            graph.add_router_edge(left, right)
            graph.set_edge_ospf_cost(left, right, VALUENOTSET)
        # NetComplete creates these variables when extracting its OSPF graph.
        forward = z3.Int("cost_%s_%s" % (source, destination))
        reverse = z3.Int("cost_%s_%s" % (destination, source))
        solver.add(forward == reverse, forward <= 65535, reverse <= 65535)
    by_id = dict((link["id"], link) for link in specification["links"])
    for group in specification.get("cost_groups", []):
        primary = by_id[group[0]]
        shared = z3.Int("cost_%s_%s" % (primary["source"], primary["target"]))
        for name in group[1:]:
            link = by_id[name]
            solver.add(
                z3.Int("cost_%s_%s" % (link["source"], link["target"])) == shared
            )
    # Start with a small native heuristic seed. Upstream's exact shortest-path
    # check still adds every discovered counterexample before accepting a model.
    synthesizer = OSPFSyn(
        graph, solver=solver, gen_paths=1, random_obj=random.Random(1)
    )
    for path in specification["paths"]:
        synthesizer.add_req(PathReq(Protocols.OSPF, path[-1], path, False))
    for paths in specification.get("ecmp_paths", []):
        reqs = [PathReq(Protocols.OSPF, path[-1], path, False) for path in paths]
        synthesizer.add_req(ECMPPathsReq(Protocols.OSPF, paths[0][-1], reqs, False))
    # Upstream reset_solver discards caller-supplied sketch equalities. Keep
    # refining the same constrained solver; the outer runner bounds execution.
    if not synthesizer.synthesize(retries_before_rest=float("inf")):
        raise RuntimeError("NetComplete could not satisfy the original paths")
    if synthesizer.removed_reqs:
        raise RuntimeError("NetComplete removed an original requirement")
    costs = dict(
        ((src, dst), cost) for src, dst, cost in synthesizer.get_output_configs()
    )
    output = []
    for link in specification["links"]:
        cost = costs[(link["source"], link["target"])]
        if cost != costs[(link["target"], link["source"])]:
            raise ValueError("Result violates the symmetric-link sketch")
        if not 1 <= cost <= 65535:
            raise ValueError("Result is outside the supported OSPF metric range")
        output.append(dict(link, metric=cost))
    return dict((link["id"], link["metric"]) for link in output)


def synthesize_bgp(specification):
    from tekton.bgp import (
        Announcement,
        BGP_ATTRS_ORIGIN,
        RouteMap,
        RouteMapLine,
        MatchIpPrefixListList,
        IpPrefixList,
        Access,
        ActionSetLocalPref,
    )
    from synet.netcomplete import NetComplete, NetCompleteConfigs
    from synet.synthesis.connected import ConnectedSyn
    from synet.utils.common import PathOrderReq
    from tekton.utils import is_empty

    peers = specification["peers"]
    local = specification["local_prefixes"]
    prefixes = sorted(set(local + [p for peer in peers for p in peer["prefixes"]]))
    preferences = dict(
        (item["prefix"], item["peers"]) for item in specification["exit_preferences"]
    )
    policies = dict(
        (peer["name"], dict(peer=peer["name"], import_terms=[], export_terms=[]))
        for peer in peers
    )
    domain = "nika_domain"
    # Exact-prefix rules have no shared match/action variables. Solving each
    # disjoint traffic class separately avoids upstream's path-only cache
    # aliasing across prefixes; final verification checks the composed result.
    for prefix in prefixes:
        graph = NetworkGraph()
        graph.add_router(domain)
        graph.set_bgp_asnum(domain, specification["asn"])
        owners = (
            [domain]
            if prefix in local
            else [peer["name"] for peer in peers if prefix in peer["prefixes"]]
        )
        owners = preferences.get(prefix, owners)
        for peer in peers:
            name = peer["name"]
            graph.add_peer(name)
            graph.set_bgp_asnum(name, peer["asn"])
            graph.add_peer_edge(domain, name)
            graph.add_peer_edge(name, domain)
            graph.add_bgp_neighbor(domain, name)
            for direction in ["in", "out"]:
                plist = IpPrefixList(
                    "prefix_%s_%s" % (name, direction), Access.permit, [prefix]
                )
                graph.add_ip_prefix_list(domain, plist)
                fixed_deny = name not in owners if direction == "in" else name in owners
                action = (
                    [ActionSetLocalPref(VALUENOTSET)]
                    if direction == "in" and not fixed_deny
                    else []
                )
                line = RouteMapLine(
                    [MatchIpPrefixListList(plist)],
                    action,
                    Access.deny if fixed_deny else VALUENOTSET,
                    10,
                )
                rmap = RouteMap(
                    "policy_%s_%s" % (name, direction),
                    [line, RouteMapLine([], [], Access.deny, 65535)],
                )
                graph.add_route_map(domain, rmap)
                method = (
                    graph.add_bgp_import_route_map
                    if direction == "in"
                    else graph.add_bgp_export_route_map
                )
                method(domain, name, rmap.name)
        announcements = []
        for owner in owners:
            announcement = Announcement(
                prefix,
                peer=owner,
                origin=BGP_ATTRS_ORIGIN.INCOMPLETE,
                as_path=[],
                as_path_len=0,
                next_hop="0.0.0.0",
                local_pref=100,
                med=100,
                communities={},
                permitted=True,
            )
            graph.add_bgp_advertise(owner, announcement)
            announcements.append(announcement)
        customer_origin = prefix in local or any(
            peer["relationship"] == "customer" and peer["name"] in owners
            for peer in peers
        )
        sources = [domain] + [
            peer["name"]
            for peer in peers
            if customer_origin or peer["relationship"] == "customer"
        ]
        requirements = []
        for source in sources:
            if source in owners:
                continue
            paths = []
            for owner in owners:
                path = (
                    [source]
                    + ([] if source == domain else [domain])
                    + ([] if owner == domain else [owner])
                )
                paths.append(PathReq(Protocols.BGP, prefix, path, False))
            requirements.append(
                PathOrderReq(Protocols.BGP, prefix, paths, False)
                if len(paths) > 1
                else paths[0]
            )
        ConnectedSyn([], graph, full=True).synthesize()
        synthesizer = NetComplete(
            requirements, graph, announcements, NetCompleteConfigs(bgp_smt=None)
        )
        synthesizer.synthesize_bgp()
        for peer in peers:
            name = peer["name"]
            for direction, field in [("in", "import_terms"), ("out", "export_terms")]:
                line = graph.get_route_maps(domain)[
                    "policy_%s_%s" % (name, direction)
                ].lines[0]
                if is_empty(line.access):
                    raise RuntimeError("NetComplete left an external policy undecided")
                preference = line.actions[0].value if line.actions else None
                if preference is not None and (
                    not isinstance(preference, int) or not 1 <= preference <= 4294967295
                ):
                    raise ValueError("Unsupported synthesized local preference")
                policies[name][field].append(
                    dict(
                        prefix=prefix,
                        permit=line.access == Access.permit,
                        local_preference=preference,
                    )
                )
    return [policies[name] for name in sorted(policies)]


def synthesize_bgp_graph(specification):
    """Complete device-level export decisions for one disjoint prefix at a time."""
    from tekton.bgp import (
        Announcement,
        BGP_ATTRS_ORIGIN,
        RouteMap,
        RouteMapLine,
        MatchIpPrefixListList,
        IpPrefixList,
        Access,
    )
    from synet.netcomplete import NetComplete, NetCompleteConfigs
    from synet.synthesis.connected import ConnectedSyn
    from synet.utils.common import KConnectedPathsReq
    from tekton.utils import is_empty

    import networkx as nx

    result = []
    for traffic in specification["traffic"]:
        graph = NetworkGraph()
        for node, asn in specification["routers"].items():
            graph.add_router(node)
            graph.set_bgp_asnum(node, asn)
        for link in specification["links"]:
            left, right = link
            graph.add_router_edge(left, right)
            graph.add_router_edge(right, left)
            graph.add_bgp_neighbor(left, right)
        if "paths" not in traffic:
            traffic["paths"] = [
                list(nx.all_shortest_paths(graph, node, traffic["origin"]))
                for node in sorted(specification["routers"])
                if node != traffic["origin"]
            ]
        required_edges = set(
            (b, a)
            for paths in traffic["paths"]
            for path in paths
            for a, b in zip(path, path[1:])
        )
        for node in specification["routers"]:
            for peer in graph.get_bgp_neighbors(node):
                plist = IpPrefixList(
                    "prefix_" + peer, Access.permit, [traffic["prefix"]]
                )
                graph.add_ip_prefix_list(node, plist)
                line = RouteMapLine(
                    [MatchIpPrefixListList(plist)],
                    [],
                    VALUENOTSET if (node, peer) in required_edges else Access.deny,
                    10,
                )
                rmap = RouteMap("export_" + peer, [line])
                graph.add_route_map(node, rmap)
                graph.add_bgp_export_route_map(node, peer, rmap.name)
        announcement = Announcement(
            traffic["prefix"],
            peer=traffic["origin"],
            origin=BGP_ATTRS_ORIGIN.INCOMPLETE,
            as_path=[],
            as_path_len=0,
            next_hop="0.0.0.0",
            local_pref=100,
            med=100,
            communities={},
            permitted=True,
        )
        graph.add_bgp_advertise(traffic["origin"], announcement)
        requirements = []
        for paths in traffic["paths"]:
            reqs = [
                PathReq(Protocols.BGP, traffic["prefix"], path, False) for path in paths
            ]
            requirements.append(
                KConnectedPathsReq(Protocols.BGP, traffic["prefix"], reqs, False)
            )
        ConnectedSyn([], graph, full=True).synthesize()
        synthesizer = NetComplete(
            requirements, graph, [announcement], NetCompleteConfigs(bgp_smt=None)
        )
        # Use the upstream propagation and constraint builder unchanged. Read
        # only configuration decisions: evaluating every intermediate symbolic
        # route is unnecessary and grows expensive on multi-pod fabrics.
        from synet.synthesis.new_propagation import EBGPPropagation

        context = synthesizer._create_context(create_as_paths=False)
        propagation = EBGPPropagation(requirements, graph, context)
        if propagation.compute_dags():
            raise RuntimeError("Unimplementable device-level BGP path order")
        propagation.synthesize()
        solver = z3.Solver(ctx=context.z3_ctx)
        if context.check(solver, track=True, set_model=False, out_smt=None) != z3.sat:
            raise RuntimeError("NetComplete could not satisfy the BGP sketch")
        model = solver.model()
        for node in specification["routers"]:
            maps = propagation.ibgp_propagation.node[node]["box"].rmaps
            for peer in graph.get_bgp_neighbors(node):
                name = "export_" + peer
                if name in maps:
                    action = maps[name].smt_lines[0].smt_actions.smt_actions[0]
                    action.value.eval(model)
                    access = action.get_config()
                else:
                    access = graph.get_route_maps(node)[name].lines[0].access
                if is_empty(access):
                    raise RuntimeError("Unresolved BGP export decision")
                result.append(
                    dict(
                        node=node,
                        peer=peer,
                        prefix=traffic["prefix"],
                        permit=access == Access.permit,
                    )
                )
    return result


def main():
    with open(sys.argv[1]) as source:
        # The pinned Python 2 bindings require byte strings for SMT names.
        specification = json.loads(
            source.read(),
            object_hook=lambda row: dict((str(k), v) for k, v in row.items()),
        )

    def strings(value):
        if isinstance(value, basestring):  # noqa: F821 - Python 2 container entry point
            return str(value)
        if isinstance(value, list):
            return [strings(item) for item in value]
        if isinstance(value, dict):
            return dict((key, strings(item)) for key, item in value.items())
        return value

    specification = strings(specification)
    if specification.get("kind") == "bgp_graph":
        result = synthesize_bgp_graph(specification)
    elif specification.get("kind") == "ospf_area":
        result = synthesize_ospf(specification)
    elif specification.get("kind") == "bgp_import_order":
        result = synthesize_bgp(specification)
    else:
        raise ValueError(
            "Unsupported routing completion kind: %s" % specification.get("kind")
        )
    with open(sys.argv[2], "w") as destination:
        json.dump(
            result,
            destination,
            indent=2,
            sort_keys=True,
        )
        destination.write("\n")


if __name__ == "__main__":
    main()
