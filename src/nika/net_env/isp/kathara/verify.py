"""Startup verification for the ISP scenario."""

from __future__ import annotations

import json
from collections import defaultdict
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
from nika.net_env.isp.igp.frr import isis_net_from_router_id
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.isp.verify_support import (
    sample_traffic,
    intent_command,
    intent_commands,
)
from nika.net_env.isp.fib import LINUX_FIB_COMMAND, linux_fib, verify_fib
from nika.net_env.verify_collection import VerifyCollection
from nika.service.pingmesh.parser import parse_ping_output
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    frr_active_or_heal,
    frr_bgp_established_peers,
    frr_ospf_full_router_ids,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
)
from nika.utils.parallel import bounded_parallel_map
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    ping_command,
)


def _collect(runtime, plan, bgp_plan, traffic, contract, healed, *, full):
    commands = []
    for node in plan.nodes:
        device = node.device_name
        commands.extend(
            [
                VerifyCommand(device, "systemctl is-active frr"),
                VerifyCommand(device, "pgrep -x zebra"),
            ]
        )
        commands.append(
            VerifyCommand(
                device,
                "vtysh -c 'show isis neighbor'"
                if plan.igp == "isis"
                else "vtysh -c 'show ip ospf neighbor'",
            )
        )
        if full:
            commands.extend(
                [
                    VerifyCommand(device, "ip -j -4 addr show", 10),
                    VerifyCommand(device, LINUX_FIB_COMMAND),
                ]
            )
        if bgp_plan:
            commands.extend(
                [
                    VerifyCommand(device, "vtysh -c 'show bgp summary'"),
                    VerifyCommand(device, _BGP_TABLE),
                ]
            )
    commands.extend(
        VerifyCommand(source, ping_command(target), 15)
        for source, target in _loopback_probes(plan)
    )
    if traffic and full:
        by_router = {host.router_device: host for host in traffic.hosts}
        for host in traffic.hosts:
            commands.extend(
                [
                    VerifyCommand(
                        host.host_name, f"ip -4 -o addr show dev {host.host_iface}", 10
                    ),
                    VerifyCommand(host.host_name, ping_command(host.gateway), 15),
                ]
            )
        for component in igp_components(plan):
            hosts = [by_router[router] for router in component if router in by_router]
            if len(hosts) >= 2:
                commands.append(
                    VerifyCommand(
                        hosts[0].host_name, ping_command(hosts[-1].address), 15
                    )
                )
    if contract:
        commands.extend(intent_commands(contract))
    if bgp_plan:
        if bgp_plan.inventory.get("rpki"):
            commands.append(
                VerifyCommand(
                    str(bgp_plan.inventory["rov_observer"]),
                    "vtysh -c 'show rpki cache-connection'",
                )
            )
        if full and bgp_plan.inventory.get("rtbh"):
            info = bgp_plan.inventory
            provider, prefix = (
                str(info["rtbh_provider_device"]),
                str(info["target_prefix"]),
            )
            commands.extend(
                [
                    VerifyCommand(
                        provider, f"vtysh -c 'show bgp ipv4 unicast {prefix}'"
                    ),
                    VerifyCommand(provider, f"vtysh -c 'show ip route {prefix}'"),
                ]
            )
            if traffic and info.get("data_plane_observer_host"):
                commands.append(
                    VerifyCommand(
                        str(info["data_plane_observer_host"]),
                        ping_command(str(info["target_ping_address"]), count=3),
                        15,
                    )
                )
    observed = collect_verify_commands(runtime, commands)
    refresh = set()
    if healed is not None:
        for node in plan.nodes:
            device = node.device_name
            if (
                observed.exec(device, "systemctl is-active frr") != "active"
                and observed.exec(device, "pgrep -x zebra")
                and device not in healed
            ):
                frr_active_or_heal(runtime, device, healed)
                refresh.add(device)
    if (
        bgp_plan
        and bgp_plan.inventory.get("rpki")
        and not _rpki_rtr_ok(observed, bgp_plan)
    ):
        observer = str(bgp_plan.inventory["rov_observer"])
        exec_or_empty(
            runtime,
            observer,
            "vtysh -c 'rpki start' >/dev/null 2>&1 || true",
            timeout=20,
        )
        refresh.add(observer)
    if refresh:
        observed = observed.merged(
            collect_verify_commands(
                runtime,
                [command for command in commands if command.node in refresh],
                nodes=observed.nodes,
            )
        )
    return observed


def verify_isp_lab_startup(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
    frr_healed: set[str] | None = None,
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
    runtime = _collect(runtime, plan, bgp_plan, None, None, frr_healed, full=False)
    check_functions = {
        "nodes_deployed": lambda: nodes_deployed(runtime, expected),
        "frr_active": lambda: _frr_active(runtime, plan, frr_healed),
        "igp_adjacencies": lambda: _igp_adjacencies_ok(runtime, plan),
        # Adjacency alone is a false ready signal on FRR 10.x: LSPs can lack
        # reachability TLVs until config fully applies and SPF runs.
        "loopbacks_reachable": lambda: _loopbacks_reachable(runtime, plan),
    }
    if bgp_plan is not None:
        check_functions["bgp_sessions"] = lambda: _bgp_sessions_ok(runtime, bgp_plan)
        check_functions["bgp_prefixes_propagated"] = lambda: (
            _bgp_prefixes_propagated_ok(runtime, bgp_plan)
        )
        if bgp_plan.inventory.get("rpki"):
            check_functions["rpki_rtr_connected"] = lambda: _rpki_rtr_ok(
                runtime, bgp_plan
            )
    checks = dict(
        zip(
            check_functions,
            [fn() for fn in check_functions.values()],
            strict=True,
        )
    )
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={
                "topology_name": plan.topology_name,
                "igp": plan.igp,
                "bgp_mode": bgp_plan.mode if bgp_plan is not None else "none",
            },
        )
    )


def verify_isp_lab(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
    contract: ValidationContract | None = None,
    frr_healed: set[str] | None = None,
) -> dict[str, Any]:
    expected = [node.device_name for node in plan.nodes]
    if traffic is not None:
        expected.extend(h.host_name for h in traffic.hosts)
    if bgp_plan is not None and bgp_plan.inventory.get("rpki"):
        rtr = bgp_plan.inventory.get("rpki_rtr") or {}
        machine = rtr.get("machine")
        if machine:
            expected.append(str(machine))
    all_hosts = [host.host_name for host in traffic.hosts] if traffic else []
    traffic = sample_traffic(traffic, contract, bgp_plan)
    runtime = _collect(
        runtime, plan, bgp_plan, traffic, contract, frr_healed, full=True
    )
    check_functions = {
        "nodes_deployed": lambda: nodes_deployed(runtime, expected),
        "frr_active": lambda: _frr_active(runtime, plan, frr_healed),
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
            [fn() for fn in check_functions.values()],
            strict=True,
        )
    )
    fibs, fib_errors = {}, {}
    for node in plan.nodes:
        try:
            fibs[node.device_name] = linux_fib(
                runtime.json(node.device_name, LINUX_FIB_COMMAND)
            )
        except (ValueError, TypeError, KeyError) as exc:
            fib_errors[node.device_name] = str(exc)
    fib_report = verify_fib(plan, fibs, errors=fib_errors)
    checks["fib_all_pairs_reachable"] = fib_report["all_pairs_reachable"]
    checks["fib_shortest_paths"] = fib_report["shortest_paths"]
    details["fib"] = fib_report
    details["host_coverage"] = {
        "total": len(all_hosts),
        "checked": [host.host_name for host in traffic.hosts] if traffic else [],
        "selection": "component endpoints and explicit contract/RTBH endpoints",
    }
    if contract is not None:
        report = verify_isp_contract(runtime, contract=contract, plan=plan)
        checks["contract_required_intents"] = report.status == "passed"
        details["validation"] = report.model_dump(mode="json")
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details=details,
        )
    )


def verify_isp_contract(
    runtime: LabRuntime,
    *,
    contract: ValidationContract,
    plan: IspPlan | None,
) -> ValidationReport:
    """Evaluate original ISP requirements against observed runtime behavior."""
    if not isinstance(runtime, VerifyCollection):
        runtime = collect_verify_commands(runtime, intent_commands(contract))
    command_cache: dict[tuple[str, str], str] = {}
    results = [
        _verify_intent(runtime, intent=intent, plan=plan, command_cache=command_cache)
        for intent in contract.intents
    ]
    return ValidationReport.from_results(contract, "isp-runtime", results)


def _verify_intent(
    runtime: LabRuntime,
    *,
    intent: ValidationIntent,
    plan: IspPlan | None,
    command_cache: dict[tuple[str, str], str],
) -> ValidationResult:
    started = perf_counter()
    result = None
    observation_duration = 0.0
    request = None
    try:
        request = intent_command(intent)
        result = runtime.results[(request.node, request.command)]
        key = request.node, request.command
        observation_duration = 0 if key in command_cache else result.duration_ms
        command_cache[key] = result.output
        if result.error:
            raise ValueError(result.error)
        if intent.route_next_hops is not None:
            expected = intent.route_next_hops
            output = runtime.json(request.node, request.command)
            actual = {
                hop["ip"]
                for route in output.get(expected.prefix, [])
                if route.get("selected")
                and route.get("destSelected")
                and route.get("installed")
                for hop in route.get("nexthops", [])
                if hop.get("fib") and hop.get("active") and "ip" in hop
            }
            passed = bool(actual) and actual == set(expected.next_hops)
            evidence = {
                "observed_next_hops": sorted(actual),
                "expected_next_hops": list(expected.next_hops),
            }
            reason = (
                None
                if passed
                else "Installed next hops differ from the original requirement"
            )
        elif intent.property in {"bgp_route", "route_policy"}:
            output = runtime.json(request.node, request.command)
            if intent.bgp_route is not None:
                expected = intent.bgp_route
                selected = [
                    path
                    for path in output.get("paths", [])
                    if path.get("bestpath") or path.get("multipath")
                ]
                paths = [
                    path.get("aspath", {}).get("string", path.get("path", ""))
                    for path in selected
                ]
                evidence = {
                    "prefix": expected.prefix,
                    "observed_present": bool(selected),
                    "expected_present": expected.present,
                    "selected_as_paths": paths,
                    "expected_exit_asn": expected.exit_asn,
                    "expected_origin_asn": expected.origin_asn,
                }
                passed = bool(selected) == expected.present and all(
                    (
                        expected.exit_asn is None
                        or path.split()[:1] == [str(expected.exit_asn)]
                    )
                    and (
                        expected.origin_asn is None
                        or path.split()[-1:] == [str(expected.origin_asn)]
                    )
                    for path in paths
                )
            else:
                expected = intent.route_policy
                available = runtime.json(
                    expected.node,
                    f"vtysh -c 'show bgp ipv4 unicast {expected.prefix} json'",
                )
                if not any(
                    path.get("valid")
                    and (path.get("bestpath") or path.get("multipath"))
                    for path in available.get("paths", [])
                ):
                    raise ValueError(
                        "No valid selected input route exists to exercise the export requirement."
                    )
                if "advertisedRoutes" not in output:
                    raise ValueError(
                        "FRR did not return advertised-route observations."
                    )
                advertised = expected.prefix in output["advertisedRoutes"]
                passed = advertised == expected.permit
                evidence = {
                    "prefix": expected.prefix,
                    "peer": expected.peer,
                    "advertised": advertised,
                    "expected_permit": expected.permit,
                }
            reason = (
                None
                if passed
                else "observed routing behavior differs from the original requirement"
            )
        elif intent.property == "adjacency":
            passed, evidence, reason = _verify_adjacency(runtime, intent, command_cache)
        elif intent.property == "waypoint":
            if plan is None:
                raise ValueError(
                    "Runtime waypoint checks require an explicit router/interface plan"
                )
            passed, evidence, reason = _verify_waypoint(runtime, intent, plan)
        else:
            passed, evidence, reason = _verify_connectivity(runtime, intent)
        status = "passed" if passed else "failed"
    except Exception as exc:  # noqa: BLE001 - one intent must not hide other evidence
        status = "error"
        evidence = (
            {
                "command": request.command,
                "source": request.node,
                "output": result.output,
                "returncode": result.returncode,
            }
            if request and result
            else {}
        )
        reason = str(exc)
    return ValidationResult(
        intent=intent.id,
        verifier="isp-runtime",
        status=status,
        evidence=evidence,
        reason=reason,
        duration_ms=observation_duration + (perf_counter() - started) * 1000,
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
    request = intent_command(intent)
    if protocol in {"tcp", "udp"}:
        assert intent.traffic.destination_port is not None
        command = request.command
        output = exec_or_empty(runtime, source, command, timeout=10)
        reachable = "PORT_OPEN" in output
    else:
        command = request.command
        output = exec_or_empty(runtime, source, command, timeout=15)
        stats = parse_ping_output(output)
        if stats["status"] == "unknown":
            raise ValueError("unable to determine reachability from probe output")
        reachable = (stats.get("rx") or 0) > 0
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
        established = sorted(frr_bgp_established_peers(output))
        passed = adjacency.remote_address in established
        evidence = {
            "local_node": adjacency.local_node,
            "remote_node": adjacency.remote_node,
            "peer_address": adjacency.remote_address,
            "command": command,
            "output": output,
            "established_peers": established,
        }
        if passed and adjacency.route_reflector_client is not None:
            role_command = "vtysh -c 'show bgp neighbors json'"
            neighbors = json.loads(
                _cached_exec(
                    runtime,
                    adjacency.local_node,
                    role_command,
                    command_cache,
                    timeout=20,
                )
            )
            family = neighbors[adjacency.remote_address]["addressFamilyInfo"][
                "ipv4Unicast"
            ]
            observed_role = family.get("routeReflectorClient", False)
            passed = passed and observed_role == adjacency.route_reflector_client
            evidence.update(
                expected_rr_client=adjacency.route_reflector_client,
                observed_rr_client=observed_role,
            )
    elif adjacency.protocol == "ospf":
        command = "vtysh -c 'show ip ospf neighbor'"
        output = _cached_exec(
            runtime,
            adjacency.local_node,
            command,
            command_cache,
            timeout=20,
        )
        # Router ID, priority and state precede the peer address. Excluding
        # those fields also handles a Router ID equal to another link's IP.
        peer_rows = "\n".join(
            line
            for line in output.splitlines()
            if adjacency.remote_address in line.split()[3:]
        )
        full_router_ids = frr_ospf_full_router_ids(peer_rows)
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
    else:
        command = "vtysh -c 'show isis neighbor'"
        output = _cached_exec(
            runtime,
            adjacency.local_node,
            command,
            command_cache,
            timeout=20,
        )
        neighbors = _parse_frr_isis_neighbors(output)
        expected_system_id = ".".join(
            isis_net_from_router_id(adjacency.remote_router_id).split(".")[2:-1]
        )
        matching = [
            neighbor
            for neighbor in neighbors
            if neighbor["system_id"].casefold()
            in {adjacency.remote_node.casefold(), expected_system_id.casefold()}
            and neighbor["interface"] == adjacency.local_interface
            and neighbor["level"] in {"2", "L2"}
            and neighbor["state"].casefold() == "up"
        ]
        passed = bool(matching)
        evidence = {
            "local_node": adjacency.local_node,
            "local_interface": adjacency.local_interface,
            "remote_node": adjacency.remote_node,
            "remote_interface": adjacency.remote_interface,
            "peer_system_id": expected_system_id,
            "command": command,
            "output": output,
            "neighbors": neighbors,
            "matching_level2_up_neighbors": matching,
            "semantic_oracle": "runtime",
        }
    return (
        passed,
        evidence,
        None
        if passed
        else "expected adjacency or declared RR/client relation was not satisfied",
    )


def _parse_frr_isis_neighbors(output: str) -> list[dict[str, str]]:
    """Parse the table from FRR ``show isis neighbor``.

    A valid header with no rows means there are no current neighbors. Output
    without the expected table header is a command or parser error and must not
    be mistaken for a legitimate down adjacency.
    """
    lines = output.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if {"system", "id", "interface", "state"}.issubset(
                {field.casefold() for field in line.split()}
            )
        ),
        None,
    )
    if header_index is None:
        raise ValueError("malformed FRR IS-IS neighbor output: table header not found")

    neighbors: list[dict[str, str]] = []
    for line in lines[header_index + 1 :]:
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 4 or fields[2].upper() not in {"1", "2", "L1", "L2"}:
            raise ValueError(f"malformed FRR IS-IS neighbor row: {line.strip()!r}")
        neighbors.append(
            {
                "system_id": fields[0],
                "interface": fields[1],
                "level": fields[2].upper(),
                "state": fields[3],
            }
        )
    return neighbors


def _verify_waypoint(
    runtime: LabRuntime, intent: ValidationIntent, plan: IspPlan
) -> tuple[bool, dict[str, Any], str | None]:
    assert intent.source is not None
    assert intent.destination is not None
    assert intent.path is not None
    source = _runtime_source(intent.source)
    target = _probe_address(intent.destination)
    command = intent_command(intent).command
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
    reached_target = False
    for line in output.splitlines():
        fields = line.split()
        for field in fields[1:]:
            try:
                address = str(IPv4Address(field))
            except ValueError:
                continue
            reached_target = reached_target or address == target
            node = address_to_node.get(address)
            if node and node not in hops:
                hops.append(node)
            break
    required = set(intent.path.must_traverse)
    forbidden = set(intent.path.must_avoid)
    passed = (
        reached_target and required.issubset(hops) and not forbidden.intersection(hops)
    )
    evidence = {
        "source": source,
        "target": target,
        "command": command,
        "output": output,
        "observed_nodes": hops,
        "reached_destination": reached_target,
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


def _frr_active(
    runtime: LabRuntime, plan: IspPlan, healed: set[str] | None = None
) -> bool:
    return all(
        bounded_parallel_map(
            lambda node: frr_active_or_heal(runtime, node.device_name, healed),
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


def _loopback_probes(plan):
    loopbacks = {node.device_name: node.loopback for node in plan.nodes}
    return [
        (component[0], loopbacks[peer])
        for component in igp_components(plan)
        for peer in component
    ]


def _loopbacks_reachable(runtime: LabRuntime, plan: IspPlan) -> bool:
    return all(
        ping_ok(runtime, source, target) for source, target in _loopback_probes(plan)
    )


def _inventory_addresses_ok(runtime: VerifyCollection, plan: IspPlan) -> bool:
    for node in plan.nodes:
        try:
            addresses = {
                row["ifname"]: {
                    entry["local"]
                    for entry in row.get("addr_info", [])
                    if entry.get("family") == "inet"
                }
                for row in runtime.json(node.device_name, "ip -j -4 addr show")
            }
        except (ValueError, KeyError, TypeError):
            return False
        if node.loopback not in addresses.get("lo", set()):
            return False
        if any(
            interface.address not in addresses.get(interface.name, set())
            for interface in node.interfaces
        ):
            return False
    return True


def _stub_hosts_addressed_ok(
    runtime: LabRuntime, traffic: IspTrafficAttachment
) -> bool:
    for host in traffic.hosts:
        if not host_has_ipv4(runtime, host.host_name, host.address, intf="eth0"):
            return False
    for edge in traffic.edge_links:
        rows = runtime.json(edge.router_device, "ip -j -4 addr show")
        if not any(
            row["ifname"] == edge.router_iface
            and any(
                entry.get("local") == edge.router_address
                for entry in row.get("addr_info", [])
            )
            for row in rows
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
        if len(hosts) >= 2 and not ping_ok(
            runtime, hosts[0].host_name, hosts[-1].address, count=1
        ):
            return False
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
        established = frr_bgp_established_peers(output)
        return peers.issubset(established)

    return all(bounded_parallel_map(device_ok, needed.items()))


_BGP_TABLE = "vtysh -c 'show bgp ipv4 unicast json'"


def _bgp_routes(runtime, device):
    data = runtime.json(device, _BGP_TABLE)
    if not isinstance(data.get("routes"), dict):
        raise ValueError(f"missing BGP route table on {device}")
    return data["routes"]


def _bgp_prefixes_originated_ok(runtime, bgp_plan):
    try:
        return all(
            bool(_bgp_routes(runtime, origin.device).get(origin.prefix))
            for origin in bgp_plan.originated
        )
    except (ValueError, KeyError, TypeError):
        return False


def _bgp_prefixes_propagated_ok(runtime, bgp_plan):
    # Propagation is a control-plane property. An unbound ping may choose an
    # infrastructure source that another AS intentionally does not import.
    # Data-plane checks use the explicit endpoints in the validation contract.
    try:
        return all(
            any(
                path.get("valid") and path.get("bestpath")
                for path in _bgp_routes(runtime, observer).get(prefix, [])
            )
            for observer, prefix in bgp_plan.expect_reachable
        )
    except (ValueError, KeyError, TypeError):
        return False


def _bgp_infra_denied_ok(runtime, bgp_plan):
    try:
        denied = [IPv4Network(prefix) for prefix in bgp_plan.deny_prefixes]
        return not any(
            IPv4Network(prefix).subnet_of(network)
            for device in {session.local_device for session in bgp_plan.sessions}
            for prefix in _bgp_routes(runtime, device)
            for network in denied
        )
    except (ValueError, KeyError, TypeError):
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


def _rpki_output_ok(output: str, routinator_address: str) -> bool:
    lowered = output.lower()
    if "unknown command" in lowered or "failed to connect" in lowered:
        return False
    return (
        "connected" in lowered
        or "establ" in lowered
        or "up" in lowered
        or (routinator_address and routinator_address in output)
        or "rtr" in lowered
    )


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
    if _rpki_output_ok(output, routinator_address):
        return True
    return False


def _rpki_leak_absent_ok(runtime, bgp_plan):
    prefixes = [str(prefix) for prefix in bgp_plan.inventory.get("leak_prefixes", [])]
    observers = [
        bgp_plan.inventory.get(key) for key in ("rov_observer", "non_rov_observer")
    ]
    if not prefixes or not all(observers):
        return False
    try:
        return not any(
            path
            for observer in observers
            for prefix in prefixes
            for path in _bgp_routes(runtime, observer).get(prefix, [])
            if not path.get("sourced", False)
        )
    except (ValueError, KeyError, TypeError):
        return False
