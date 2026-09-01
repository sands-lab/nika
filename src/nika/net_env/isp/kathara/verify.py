"""Startup verification for the ISP scenario."""

from __future__ import annotations

from collections import defaultdict, deque
from ipaddress import IPv4Address, IPv4Network
from time import perf_counter
from typing import Any

from nika.net_env.contract import (
    NetworkEntity,
    ValidationContract,
    ValidationIntent,
    ValidationReport,
    ValidationResult,
)
from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.igp.plan import IspPlan, active_igp_links, igp_components
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.verify import (
    bounded_parallel_map,
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
    service_active,
)
from nika.runtime.base import LabRuntime


def verify_isp_lab_startup(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
) -> dict[str, Any]:
    """Bounded readiness: deployment, FRR, IGP adjacency, and BGP sessions."""
    expected = [node.device_name for node in plan.nodes]
    if traffic is not None:
        expected.extend(h.host_name for h in traffic.hosts)
    if bgp_plan is not None and bgp_plan.inventory.get("rpki"):
        rtr = bgp_plan.inventory.get("rpki_rtr") or {}
        machine = rtr.get("machine")
        if machine:
            expected.append(str(machine))
    check_functions = {
        "nodes_deployed": lambda: nodes_deployed(runtime, expected),
        "frr_active": lambda: _frr_active(runtime, plan),
        "igp_adjacencies": lambda: _igp_adjacencies_ok(runtime, plan),
    }
    if bgp_plan is not None:
        check_functions["bgp_sessions"] = lambda: _bgp_sessions_ok(runtime, bgp_plan)
    checks = dict(
        zip(
            check_functions,
            bounded_parallel_map(lambda fn: fn(), check_functions.values()),
            strict=True,
        )
    )
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "topology_name": plan.topology_name,
            "igp": plan.igp,
            "bgp_mode": bgp_plan.mode if bgp_plan is not None else "none",
        },
    )


def verify_isp_lab(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
    contract: ValidationContract | None = None,
) -> dict[str, Any]:
    expected = [node.device_name for node in plan.nodes]
    if traffic is not None:
        expected.extend(h.host_name for h in traffic.hosts)
    if bgp_plan is not None and bgp_plan.inventory.get("rpki"):
        rtr = bgp_plan.inventory.get("rpki_rtr") or {}
        machine = rtr.get("machine")
        if machine:
            expected.append(str(machine))
    check_functions = {
        "nodes_deployed": lambda: nodes_deployed(runtime, expected),
        "frr_active": lambda: _frr_active(runtime, plan),
        "igp_adjacencies": lambda: _igp_adjacencies_ok(runtime, plan),
        "loopbacks_reachable": lambda: _loopbacks_reachable(runtime, plan),
        "inventory_addresses": lambda: _inventory_addresses_ok(runtime, plan),
    }
    details: dict[str, Any] = {
        "topology_name": plan.topology_name,
        "igp": plan.igp,
        "node_count": len(plan.nodes),
        "link_count": len(plan.links),
        "inventory": plan.inventory,
        "bgp_mode": bgp_plan.mode if bgp_plan is not None else "none",
        "traffic_stubs": bool(
            (plan.inventory.get("traffic") or {}).get("stubs")
            or plan.inventory.get("hosts")
        ),
    }
    if traffic is not None:
        check_functions["stub_hosts_addressed"] = lambda: _stub_hosts_addressed_ok(
            runtime, traffic
        )
        check_functions["stub_gateway_reachable"] = lambda: _stub_gateway_ok(
            runtime, traffic
        )
        check_functions["stub_remote_reachable"] = lambda: _stub_remote_ok(
            runtime, traffic
        )
        details["hosts"] = plan.inventory.get("hosts")
    if bgp_plan is not None:
        check_functions["bgp_sessions"] = lambda: _bgp_sessions_ok(runtime, bgp_plan)
        check_functions["bgp_prefixes_originated"] = lambda: (
            _bgp_prefixes_originated_ok(runtime, bgp_plan)
        )
        check_functions["bgp_prefixes_propagated"] = lambda: (
            _bgp_prefixes_propagated_ok(runtime, bgp_plan)
        )
        check_functions["bgp_infra_denied"] = lambda: _bgp_infra_denied_ok(
            runtime, bgp_plan
        )
        if bgp_plan.inventory.get("rpki"):
            check_functions["rpki_rtr_connected"] = lambda: _rpki_rtr_ok(
                runtime, bgp_plan
            )
            check_functions["rpki_leak_absent"] = lambda: _rpki_leak_absent_ok(
                runtime, bgp_plan
            )
        if bgp_plan.inventory.get("rtbh"):
            check_functions["rtbh_baseline_healthy"] = lambda: (
                _rtbh_baseline_healthy_ok(runtime, bgp_plan, traffic)
            )
        details["bgp"] = bgp_plan.inventory
    checks = dict(
        zip(
            check_functions,
            bounded_parallel_map(lambda fn: fn(), check_functions.values()),
            strict=True,
        )
    )
    if contract is not None:
        report = verify_isp_contract(runtime, contract=contract, plan=plan)
        checks["contract_required_intents"] = report.status == "passed"
        details["validation"] = report.model_dump(mode="json")
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details=details,
    )


def verify_isp_contract(
    runtime: LabRuntime,
    *,
    contract: ValidationContract,
    plan: IspPlan,
) -> ValidationReport:
    """Execute an ISP contract with runtime tools without changing its semantics."""
    command_cache: dict[tuple[str, str], str] = {}
    results = [
        _verify_intent(runtime, intent=intent, plan=plan, command_cache=command_cache)
        for intent in contract.intents
    ]
    return ValidationReport.from_results(contract, "isp-runtime-v1", results)


def _verify_intent(
    runtime: LabRuntime,
    *,
    intent: ValidationIntent,
    plan: IspPlan,
    command_cache: dict[tuple[str, str], str],
) -> ValidationResult:
    started = perf_counter()
    try:
        if intent.property == "adjacency":
            passed, evidence, reason = _verify_adjacency(runtime, intent, command_cache)
        elif intent.property == "waypoint":
            passed, evidence, reason = _verify_waypoint(runtime, intent, plan)
        else:
            passed, evidence, reason = _verify_connectivity(runtime, intent)
        status = "passed" if passed else "failed"
    except Exception as exc:  # noqa: BLE001 - one intent must not hide other evidence
        status = "error"
        evidence = {}
        reason = str(exc)
    return ValidationResult(
        intent=intent.id,
        verifier="isp-runtime-v1",
        status=status,
        evidence=evidence,
        reason=reason,
        duration_ms=(perf_counter() - started) * 1000,
    )


def _verify_connectivity(
    runtime: LabRuntime, intent: ValidationIntent
) -> tuple[bool, dict[str, Any], str | None]:
    assert intent.source is not None
    assert intent.destination is not None
    assert intent.traffic is not None
    source = _runtime_source(intent.source)
    target = _probe_address(intent.destination)
    protocol = intent.traffic.protocol
    if protocol in {"tcp", "udp"}:
        assert intent.traffic.destination_port is not None
        udp_flag = "-u " if protocol == "udp" else ""
        command = (
            f"nc -z {udp_flag}-w 2 {target} {intent.traffic.destination_port} "
            "&& echo NIKA_OPEN"
        )
        output = exec_or_empty(runtime, source, command, timeout=10)
        reachable = "NIKA_OPEN" in output
    else:
        command = f"ping -c 1 -W 2 {target}"
        output = exec_or_empty(runtime, source, command, timeout=15)
        reachable = "1 received" in output or "1 packets received" in output
    passed = reachable if intent.property == "reachability" else not reachable
    evidence = {
        "source": source,
        "target": target,
        "protocol": protocol,
        "destination_port": intent.traffic.destination_port,
        "command": command,
        "output": output,
        "observed_reachable": reachable,
    }
    reason = None if passed else f"observed_reachable={reachable}"
    return passed, evidence, reason


def _verify_adjacency(
    runtime: LabRuntime,
    intent: ValidationIntent,
    command_cache: dict[tuple[str, str], str],
) -> tuple[bool, dict[str, Any], str | None]:
    assert intent.adjacency is not None
    adjacency = intent.adjacency
    if adjacency.protocol == "bgp":
        command = "vtysh -c 'show bgp summary'"
        output = _cached_exec(
            runtime,
            adjacency.local_node,
            command,
            command_cache,
            timeout=20,
        )
        established = sorted(_bgp_established_peers(output))
        passed = adjacency.remote_address in established
        evidence = {
            "local_node": adjacency.local_node,
            "remote_node": adjacency.remote_node,
            "peer_address": adjacency.remote_address,
            "command": command,
            "output": output,
            "established_peers": established,
        }
    else:
        command = "vtysh -c 'show ip ospf neighbor'"
        output = _cached_exec(
            runtime,
            adjacency.local_node,
            command,
            command_cache,
            timeout=20,
        )
        full_router_ids = _ospf_full_router_ids(output)
        remote_router_id = adjacency.remote_router_id or adjacency.remote_address
        passed = remote_router_id in full_router_ids
        evidence = {
            "local_node": adjacency.local_node,
            "remote_node": adjacency.remote_node,
            "peer_router_id": remote_router_id,
            "command": command,
            "output": output,
            "full_router_ids": sorted(full_router_ids),
        }
    return (
        passed,
        evidence,
        None if passed else "expected adjacency was not established",
    )


def _verify_waypoint(
    runtime: LabRuntime, intent: ValidationIntent, plan: IspPlan
) -> tuple[bool, dict[str, Any], str | None]:
    assert intent.source is not None
    assert intent.destination is not None
    assert intent.path is not None
    source = _runtime_source(intent.source)
    target = _probe_address(intent.destination)
    command = f"traceroute -n -m 32 -w 1 -q 1 {target}"
    output = exec_or_empty(
        runtime,
        source,
        command,
        timeout=40,
    )
    address_to_node: dict[str, str] = {}
    for node in plan.nodes:
        address_to_node[node.loopback] = node.device_name
        for interface in node.interfaces:
            address_to_node[interface.address] = node.device_name
    hops = []
    for line in output.splitlines():
        fields = line.split()
        for field in fields[1:]:
            try:
                address = str(IPv4Address(field))
            except ValueError:
                continue
            node = address_to_node.get(address)
            if node and node not in hops:
                hops.append(node)
            break
    required = set(intent.path.must_traverse)
    forbidden = set(intent.path.must_avoid)
    passed = (
        bool(output.strip())
        and required.issubset(hops)
        and not forbidden.intersection(hops)
    )
    evidence = {
        "source": source,
        "target": target,
        "command": command,
        "output": output,
        "observed_nodes": hops,
        "must_traverse": list(intent.path.must_traverse),
        "must_avoid": list(intent.path.must_avoid),
    }
    reason = None if passed else "observed path did not satisfy the path constraint"
    return passed, evidence, reason


def _runtime_source(entity: NetworkEntity) -> str:
    if entity.kind == "endpoint":
        return entity.name
    if entity.kind == "node":
        return entity.name
    if entity.node:
        return entity.node
    raise ValueError(f"entity {entity.name!r} cannot be used as a runtime source")


def _probe_address(entity: NetworkEntity) -> str:
    if not entity.address:
        raise ValueError(f"entity {entity.name!r} has no address")
    if entity.kind == "prefix":
        network = IPv4Network(entity.address)
        return str(network.network_address + 1)
    return entity.address.split("/", 1)[0]


def _ospf_full_router_ids(output: str) -> set[str]:
    peers: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and any(field.startswith("Full") for field in fields):
            try:
                peers.add(str(IPv4Address(fields[0])))
            except ValueError:
                continue
    return peers


def _cached_exec(
    runtime: LabRuntime,
    host: str,
    command: str,
    cache: dict[tuple[str, str], str],
    *,
    timeout: float,
) -> str:
    key = (host, command)
    if key not in cache:
        cache[key] = exec_or_empty(runtime, host, command, timeout=timeout)
    return cache[key]


def _igp_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    if not plan.links:
        return True
    if plan.igp == "isis":
        return _isis_adjacencies_ok(runtime, plan)
    if plan.igp == "ospf":
        return _ospf_adjacencies_ok(runtime, plan)
    return False


def _frr_active(runtime: LabRuntime, plan: IspPlan) -> bool:
    return all(
        bounded_parallel_map(
            lambda node: service_active(runtime, node.device_name, "frr"),
            plan.nodes,
        )
    )


def _isis_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    degree: dict[str, int] = defaultdict(int)
    for link in active_igp_links(plan):
        degree[link.endpoint_a] += 1
        degree[link.endpoint_b] += 1

    def node_ok(node) -> bool:
        need = degree[node.device_name]
        if need == 0:
            return True
        output = exec_or_empty(
            runtime, node.device_name, "vtysh -c 'show isis neighbor'", timeout=20
        )
        up = sum(1 for line in output.splitlines() if _isis_up(line))
        return up >= need

    return all(bounded_parallel_map(node_ok, plan.nodes))


def _isis_up(line: str) -> bool:
    fields = line.split()
    return len(fields) >= 4 and fields[3] == "Up"


def _ospf_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    degree: dict[str, int] = defaultdict(int)
    for link in active_igp_links(plan):
        degree[link.endpoint_a] += 1
        degree[link.endpoint_b] += 1

    def node_ok(node) -> bool:
        need = degree[node.device_name]
        if need == 0:
            return True
        output = exec_or_empty(
            runtime,
            node.device_name,
            "vtysh -c 'show ip ospf neighbor'",
            timeout=20,
        )
        full = 0
        for line in output.splitlines():
            fields = line.split()
            if any(field.startswith("Full") for field in fields):
                full += 1
        return full >= need

    return all(bounded_parallel_map(node_ok, plan.nodes))


def _loopbacks_reachable(runtime: LabRuntime, plan: IspPlan) -> bool:
    if not plan.nodes:
        return True
    if len(plan.nodes) == 1:
        node = plan.nodes[0]
        return ping_ok(runtime, node.device_name, node.loopback)

    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    loopback = {node.device_name: node.loopback for node in plan.nodes}
    for link in active_igp_links(plan):
        adj[link.endpoint_a].append((link.endpoint_b, loopback[link.endpoint_b]))
        adj[link.endpoint_b].append((link.endpoint_a, loopback[link.endpoint_a]))

    probes: list[tuple[str, str]] = []
    for component in igp_components(plan):
        root = component[0]
        seen = {root}
        queue: deque[str] = deque([root])
        while queue:
            device = queue.popleft()
            for peer, peer_lo in sorted(adj[device]):
                if peer in seen:
                    continue
                probes.append((device, peer_lo))
                seen.add(peer)
                queue.append(peer)
        if seen != set(component):
            return False
    return all(
        bounded_parallel_map(
            lambda probe: ping_ok(runtime, probe[0], probe[1], count=1), probes
        )
    )


def _inventory_addresses_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    def node_ok(node) -> bool:
        if not host_has_ipv4(runtime, node.device_name, node.loopback, intf="lo"):
            return False
        backbone = [i for i in node.interfaces if not i.passive]
        if backbone:
            iface = backbone[0]
            if not host_has_ipv4(
                runtime, node.device_name, iface.address, intf=iface.name
            ):
                return False
        return True

    return all(bounded_parallel_map(node_ok, plan.nodes))


def _stub_hosts_addressed_ok(
    runtime: LabRuntime, traffic: IspTrafficAttachment
) -> bool:
    for host in traffic.hosts:
        if not host_has_ipv4(runtime, host.host_name, host.address, intf="eth0"):
            return False
    for edge in traffic.edge_links:
        if not host_has_ipv4(
            runtime,
            edge.router_device,
            edge.router_address,
            intf=edge.router_iface,
        ):
            return False
    return True


def _stub_gateway_ok(runtime: LabRuntime, traffic: IspTrafficAttachment) -> bool:
    if not traffic.hosts:
        return True
    for host in traffic.hosts:
        if not ping_ok(runtime, host.host_name, host.gateway, count=1):
            return False
    return True


def _stub_remote_ok(runtime: LabRuntime, traffic: IspTrafficAttachment) -> bool:
    host_by_router = {host.router_device: host for host in traffic.hosts}
    for component in igp_components(traffic.plan):
        hosts = sorted(
            (
                host_by_router[router]
                for router in component
                if router in host_by_router
            ),
            key=lambda host: host.host_name,
        )
        if len(hosts) >= 2:
            return ping_ok(runtime, hosts[0].host_name, hosts[-1].address, count=1)
    return True


def _bgp_sessions_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """Every planned session endpoint should show Established for the peer IP."""
    needed: dict[str, set[str]] = defaultdict(set)
    for sess in bgp_plan.sessions:
        needed[sess.local_device].add(sess.remote_ip)

    def device_ok(item: tuple[str, set[str]]) -> bool:
        device, peers = item
        output = exec_or_empty(
            runtime, device, "vtysh -c 'show bgp summary'", timeout=20
        )
        established = _bgp_established_peers(output)
        return peers.issubset(established)

    return all(bounded_parallel_map(device_ok, needed.items()))


def _bgp_established_peers(summary: str) -> set[str]:
    """Parse ``show bgp summary`` neighbor lines for Established peers.

    FRR columns: Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down
    State/PfxRcd PfxSnt [Desc]. Established peers show a numeric PfxRcd.
    """
    peers: set[str] = set()
    for line in summary.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        neighbor = fields[0]
        if neighbor.count(".") != 3:
            continue
        state = fields[9]
        if state.isdigit() or state == "Established":
            peers.add(neighbor)
    return peers


def _bgp_prefixes_originated_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    for pref in bgp_plan.originated:
        output = exec_or_empty(
            runtime,
            pref.device,
            f"vtysh -c 'show bgp ipv4 unicast {pref.prefix}'",
            timeout=20,
        )
        if pref.prefix.split("/")[0] not in output and pref.prefix not in output:
            return False
    return True


def _bgp_prefixes_propagated_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """Observers learn business prefixes; ping the originator's installed host."""
    ping_by_prefix = {o.prefix: o.ping_address for o in bgp_plan.originated}
    for observer, prefix in bgp_plan.expect_reachable:
        output = exec_or_empty(
            runtime,
            observer,
            f"vtysh -c 'show bgp ipv4 unicast {prefix}'",
            timeout=20,
        )
        network = prefix.split("/")[0]
        if network not in output and prefix not in output:
            return False
        # Valid path indicator
        if "Network not in table" in output or "Unknown command" in output:
            return False
        target = ping_by_prefix.get(prefix)
        if target and not ping_ok(runtime, observer, target, count=1):
            return False
    return True


def _bgp_infra_denied_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """Infra aggregates must not appear as BGP unicast routes on border/RR peers."""
    # Sample a few devices that have BGP sessions.
    devices = sorted({s.local_device for s in bgp_plan.sessions})
    if not devices:
        return True
    sample = devices[: min(5, len(devices))]
    for device in sample:
        output = exec_or_empty(
            runtime, device, "vtysh -c 'show bgp ipv4 unicast'", timeout=20
        )
        for deny in bgp_plan.deny_prefixes:
            # Look for infra networks being carried (not just ACL text).
            # Match lines that look like route entries for 10.x prefixes.
            if _bgp_table_has_infra(output, deny):
                return False
    return True


def _bgp_table_has_infra(table: str, deny_prefix: str) -> bool:
    """Return True if the BGP table appears to carry routes in an infra prefix."""
    # deny_prefix like 10.0.0.0/8 or 10.255.0.0/16
    if deny_prefix.startswith("10.255."):
        needle_prefix = "10.255."
    elif deny_prefix.startswith("10.0."):
        needle_prefix = "10."
    else:
        needle_prefix = deny_prefix.split("/")[0]
    for line in table.splitlines():
        fields = line.split()
        if not fields:
            continue
        # Route lines often start with * or spaces then network
        net = fields[0].lstrip("*>").lstrip("*")
        if not net or net.count(".") != 3:
            # sometimes "*>  10.0.0.0/31"
            for field in fields[:3]:
                candidate = field.lstrip("*>")
                if "/" in candidate and candidate.startswith(needle_prefix):
                    # Business prefixes never use 10/8; any 10.x BGP route is infra leak.
                    if candidate.startswith("10."):
                        return True
        elif "/" in net and net.startswith("10."):
            return True
    return False


def _rtbh_baseline_healthy_ok(
    runtime: LabRuntime,
    bgp_plan: BgpPlan,
    traffic: IspTrafficAttachment | None,
) -> bool:
    """Healthy RTBH lab: no community tag, no discard forwarding, observer can ping."""
    prefix = str(bgp_plan.inventory.get("target_prefix") or "")
    community = str(bgp_plan.inventory.get("blackhole_community") or "")
    ping_addr = str(bgp_plan.inventory.get("target_ping_address") or "")
    provider = str(bgp_plan.inventory.get("rtbh_provider_device") or "")
    observer = str(bgp_plan.inventory.get("data_plane_observer_host") or "")
    discard_nh = str(bgp_plan.inventory.get("discard_next_hop") or "")
    if not prefix or not community or not ping_addr or not provider:
        return False

    route_out = exec_or_empty(
        runtime,
        provider,
        f"vtysh -c 'show bgp ipv4 unicast {prefix}'",
        timeout=20,
    )
    if community in route_out and "Community" in route_out:
        # Ignore incidental matches in unrelated output; require community attribute.
        comm_lines = [
            line
            for line in route_out.splitlines()
            if "community" in line.lower() and community in line
        ]
        if comm_lines:
            return False

    fib_out = exec_or_empty(
        runtime,
        provider,
        f"vtysh -c 'show ip route {prefix}'",
        timeout=20,
    )
    if discard_nh and discard_nh in fib_out and "blackhole" in fib_out.lower():
        return False
    if "Null0" in fib_out and prefix.split("/")[0] in fib_out:
        return False

    if traffic is not None and observer:
        return ping_ok(runtime, observer, ping_addr, count=3)
    return True


def _rpki_rtr_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """ROV observer has an established RPKI cache connection."""
    observer = str(bgp_plan.inventory.get("rov_observer") or "")
    if not observer:
        return False
    rtr = bgp_plan.inventory.get("rpki_rtr") or {}
    routinator_address = str(rtr.get("address") or "")
    output = exec_or_empty(
        runtime, observer, "vtysh -c 'show rpki cache-connection'", timeout=20
    )
    lowered = output.lower()
    if "unknown command" in lowered:
        return False
    return (
        "connected" in lowered
        or "establ" in lowered
        or "up" in lowered
        or (routinator_address and routinator_address in output)
        or "rtr" in lowered
    )


def _rpki_leak_absent_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """Healthy state: observers must not learn leak-target prefixes via BGP."""
    prefixes = [str(p) for p in (bgp_plan.inventory.get("leak_prefixes") or [])]
    observers = [
        str(bgp_plan.inventory.get("rov_observer") or ""),
        str(bgp_plan.inventory.get("non_rov_observer") or ""),
    ]
    observers = [o for o in observers if o]
    if not prefixes or not observers:
        return False
    for observer in observers:
        for prefix in prefixes:
            output = exec_or_empty(
                runtime,
                observer,
                f"vtysh -c 'show bgp ipv4 unicast {prefix}'",
                timeout=20,
            )
            if "Network not in table" in output:
                continue
            network = prefix.split("/")[0]
            if network in output or prefix in output:
                # Local aggregate noise is fine; reject learned paths.
                if "from" in output.lower() or "Path" in output or "*" in output:
                    return False
    return True
