"""Evaluate original validation contracts on IOS XR and RouterOS runtimes."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from ipaddress import IPv4Address
from time import perf_counter

from nika.net_env.contract import (
    ValidationContract,
    ValidationReport,
    ValidationResult,
)
from nika.net_env.routeros_simple_bgp.verify import CLI_COMMAND
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
)
from nika.service.pingmesh.parser import parse_ping_output

IOSXR_BGP_COMMAND = "/pkg/bin/xr_cli 'show bgp summary'"
ROUTEROS_BGP_COMMAND = CLI_COMMAND.format(
    command="/routing/bgp/session/print terse where established=yes"
)


@dataclass(frozen=True)
class _BgpPeer:
    remote_asn: int | None
    established: bool
    local_asn: int | None = None
    local_address: str | None = None
    rr_client: bool | None = None


def _parse_iosxr_bgp_summary(output: str) -> tuple[int | None, dict[str, _BgpPeer]]:
    if not output.strip() or re.search(
        r"syntax error|unknown command|invalid command|invalid input|bad command",
        output,
        re.I,
    ):
        raise ValueError("IOS XR returned invalid BGP summary output")
    local_match = re.search(r"local AS number\s+(\d+)", output, re.IGNORECASE)
    if local_match is None:
        raise ValueError("IOS XR BGP summary omitted the local AS number")
    header = next(
        (
            line.split()
            for line in output.splitlines()
            if line.split() and line.split()[0].lower() == "neighbor"
        ),
        None,
    )
    if header is None or not any(field.lower() == "as" for field in header):
        raise ValueError("IOS XR returned no recognizable BGP summary header")
    local_asn = int(local_match.group(1)) if local_match else None
    peers: dict[str, _BgpPeer] = {}
    remote_as_index = 1
    for line in output.splitlines():
        fields = line.split()
        if (
            fields
            and fields[0].lower() == "neighbor"
            and ("AS" in fields or "as" in fields)
        ):
            remote_as_index = (
                fields.index("AS") if "AS" in fields else fields.index("as")
            )
            continue
        if not fields or not re.match(r"\d+\.\d+\.\d+\.\d+$", fields[0]):
            continue
        if len(fields) <= remote_as_index or not re.fullmatch(
            r"\d+", fields[remote_as_index]
        ):
            raise ValueError(f"Malformed IOS XR BGP neighbor row: {line.strip()}")
        address, remote_asn = fields[0], int(fields[remote_as_index])
        state = fields[-1]
        established = state.isdigit() or state.lower() == "established"
        if (
            state.lower()
            not in {"idle", "active", "connect", "opensent", "openconfirm"}
            and not state.isdigit()
            and state.lower() != "established"
        ):
            raise ValueError(f"Unknown IOS XR BGP neighbor state: {state}")
        peers[address] = _BgpPeer(remote_asn, established, local_asn)
    return local_asn, peers


def _parse_routeros_bgp_sessions(output: str) -> dict[str, _BgpPeer]:
    """Parse terse RouterOS session rows, retaining remote/local AS identity."""
    if re.search(
        r"syntax error|unknown command|invalid command|invalid input|bad command|failure:",
        output,
        re.I,
    ):
        raise ValueError("RouterOS returned a CLI error for BGP session output")
    peers: dict[str, _BgpPeer] = {}
    # RouterOS terse output keeps one session on a line. Properties are emitted
    # in remote then local groups; each group's .as belongs to that group.
    for line in output.splitlines():
        address_match = re.search(r"(?:^|\s)remote\.address=([^\s]+)", line)
        if not address_match:
            continue
        remote_fields = line.split("local.", 1)[0]
        remote_as_match = re.search(r"(?:remote\.as=|\.as=)(\d+)", remote_fields)
        local_group = re.search(r"local\.", line)
        local_as_match = None
        if local_group:
            local_fields = line[local_group.start() :]
            local_as_match = re.search(r"(?:local\.as=|\.as=)(\d+)", local_fields)
        local_properties = {
            key.removeprefix("local."): value
            for key, value in _ros_properties(line.split("local.", 1)[-1]).items()
        }
        remote_asn = int(remote_as_match.group(1)) if remote_as_match else None
        local_asn = int(local_as_match.group(1)) if local_as_match else None
        role = local_properties.get("role")
        # RouterOS omits its default eBGP role in terse output. An external
        # session cannot be a route-reflector client (RFC 4456's iBGP scope).
        rr_client = (
            role == "ibgp-rr"
            if role is not None
            else False
            if local_asn is not None
            and remote_asn is not None
            and local_asn != remote_asn
            else None
        )
        # This command filters to established sessions. RouterOS versions may
        # include or omit the E flag in terse output.
        peers[address_match.group(1)] = _BgpPeer(
            remote_asn,
            True,
            local_asn,
            local_properties.get("address"),
            rr_client,
        )
    if (
        output.strip()
        and not peers
        and not re.search(r"(?:^|\n)\s*(?:Flags:|Columns:|#\s+)", output, re.I)
    ):
        raise ValueError("RouterOS returned unrecognized BGP session output")
    return peers


def _native_command(platform: str, command: str) -> str:
    if platform == "iosxr":
        return shlex.join(["/pkg/bin/xr_cli", command])
    return CLI_COMMAND.format(command=command)


def _check_output(output: str) -> None:
    if not output.strip() or re.search(
        r"syntax error|unknown command|invalid command|invalid input|bad command|failure:|expected end of command",
        output,
        re.I,
    ):
        raise ValueError("Native CLI returned missing or invalid output")


def _ros_properties(output: str) -> dict[str, str]:
    return {
        key.lstrip("."): quoted or bare
        for key, quoted, bare in re.findall(
            r'(?:^|\s)([\w.-]+)=(?:"([^"\n]*)"|([^\s]+))', output
        )
    }


def _ros_rows(output: str, key: str) -> list[dict[str, str]]:
    _check_output(output)
    rows = []
    for block in re.split(r"(?m)^\s*(?=\d+\s)", output):
        if re.search(rf"(?:^|\s){re.escape(key)}=", block):
            rows.append(_ros_properties(block))
    if not rows and not re.search(
        r"Flags:|Columns:|Total (?:number of )?(?:items|entries)", output, re.I
    ):
        raise ValueError("Unrecognized RouterOS table output")
    return rows


def _iosxr_neighbor(output: str) -> tuple[str, _BgpPeer]:
    _check_output(output)
    identity = re.search(
        r"BGP neighbor is\s+([0-9.]+),?\s+remote AS\s+(\d+),\s+local AS\s+(\d+)",
        output,
        re.I,
    )
    state = re.search(r"BGP state\s*=\s*(\w+)", output, re.I)
    source = re.search(r"Local host:\s*([\d.]+)", output, re.I)
    if not identity or not state or not source:
        raise ValueError("Incomplete IOS XR BGP neighbor identity")
    if state[1].lower() not in {
        "idle",
        "active",
        "connect",
        "opensent",
        "openconfirm",
        "established",
    }:
        raise ValueError("Unknown IOS XR BGP neighbor state")
    return identity[1], _BgpPeer(
        int(identity[2]),
        state[1].lower() == "established",
        int(identity[3]),
        source[1],
        bool(re.search(r"(?<!not a )Route[ -]Reflector Client", output, re.I)),
    )


def _route_next_hops(output: str, platform: str, prefix: str) -> set[str]:
    _check_output(output)
    if platform == "routeros":
        return {
            str(IPv4Address(hop.split("%", 1)[0]))
            for row in _ros_rows(output, "dst-address")
            if row["dst-address"] == prefix
            for hop in row.get("immediate-gw", "").split(",")
            if re.match(r"^\d+\.\d+\.\d+\.\d+%", hop)
        }
    if "Network not in table" in output:
        return set()
    if not re.search(rf"Routing entry for\s+{re.escape(prefix)}(?:\s|$)", output):
        raise ValueError("IOS XR omitted the requested exact route")
    if "Routing Descriptor Blocks" not in output:
        raise ValueError("IOS XR omitted route forwarding descriptors")
    return {
        str(IPv4Address(value))
        for value in re.findall(r"(?m)^\s*([\d.]+), from ", output)
    }


def _selected_as_paths(
    output: str, platform: str, prefix: str
) -> list[tuple[int, ...]]:
    _check_output(output)
    if platform == "routeros":
        paths = []
        for row in _ros_rows(output, "dst-address"):
            if row["dst-address"] != prefix:
                continue
            value = row.get("bgp.as-path", row.get("as-path"))
            if value is None:
                raise ValueError("Selected RouterOS BGP route omitted its AS path")
            if value and not re.fullmatch(r"\d+(?:[, ]+\d+)*", value):
                raise ValueError("AS selection checks require sequence AS paths")
            paths.append(tuple(int(asn) for asn in re.findall(r"\d+", value)))
        return paths
    if re.search(r"(?:Network|Route) not (?:in table|found)", output, re.I):
        return []
    if not re.search(
        rf"BGP routing table entry for\s+{re.escape(prefix)}(?:\s|,|$)", output
    ):
        raise ValueError("Unrecognized IOS XR BGP route output")
    paths = []
    for block in re.split(r"(?m)^\s*Path #\d+:", output)[1:]:
        if not re.search(r"(?:^|[, ])best(?:[, \n]|$)", block, re.I):
            continue
        sequence = re.search(
            r"(?m)^\s*(Local|\d+(?: +\d+)*)(?:, \(Received from a RR-client\))?\s*$",
            block,
        )
        if sequence is None:
            raise ValueError("Selected IOS XR path omitted a sequence AS path")
        paths.append(
            () if sequence[1] == "Local" else tuple(map(int, sequence[1].split()))
        )
    if not paths and not re.search(r"(?:0 available|no best path)", output, re.I):
        raise ValueError("IOS XR BGP route omitted its selected path")
    return paths


def _ospf_established(
    output: str, platform: str, adjacency, plan, *, instances=None, areas=None
) -> bool:
    _check_output(output)
    node = next(n for n in plan.nodes if n.device_name == adjacency.local_node)
    interface = next(i for i in node.interfaces if i.address == adjacency.local_address)
    index = int(interface.name.removeprefix("eth"))
    if platform == "routeros":
        if instances is None or areas is None:
            raise ValueError(
                "RouterOS OSPF identity requires instance and area observations"
            )
        local_instances = {
            row["name"]
            for row in _ros_rows(instances, "router-id")
            if row.get("router-id") == adjacency.local_router_id
        }
        local_areas = {
            row["name"]
            for row in _ros_rows(areas, "area-id")
            if row.get("instance") in local_instances
            and row.get("area-id") == "0.0.0.0"
        }
        rows = _ros_rows(output, "router-id")
        if any(
            not {"router-id", "address", "interface", "state"} <= row.keys()
            for row in rows
        ):
            raise ValueError("Incomplete RouterOS OSPF neighbor identity")
        return any(
            row.get("router-id") == adjacency.remote_router_id
            and row.get("address", "").split("%", 1)[0] == adjacency.remote_address
            and row.get("interface", "").split("%", 1)[0] == f"ether{index + 1}"
            and row.get("state", "").lower() == "full"
            and row.get("instance") in local_instances
            and row.get("area") in local_areas
            for row in rows
        )
    if not re.search(r"Neighbors for OSPF|Neighbor\s+\d+\.\d+\.\d+\.\d+", output, re.I):
        raise ValueError("Unrecognized IOS XR OSPF neighbor output")
    for block in re.split(r"(?m)(?=^\s*Neighbor\s+\d+\.)", output):
        identity = re.search(
            r"Neighbor\s+([\d.]+),\s+interface address\s+([\d.]+)", block, re.I
        )
        if not identity:
            if re.search(r"Neighbor\s+\d+\.", block):
                raise ValueError("Incomplete IOS XR OSPF neighbor identity")
            continue
        if not re.search(r"State is ", block, re.I):
            raise ValueError("IOS XR OSPF neighbor omitted its state")
        if (
            identity[1] == adjacency.remote_router_id
            and identity[2] == adjacency.remote_address
            and re.search(
                rf"(?:interface|via interface)\s+GigabitEthernet0/0/0/{index}(?:\s|,|$)",
                block,
                re.I,
            )
            and re.search(r"area\s+(?:0|0\.0\.0\.0)(?:\s|,|$)", block, re.I)
            and re.search(r"State is FULL", block, re.I)
        ):
            return True
    return False


def _native_ping_status(output: str, platform: str) -> bool:
    _check_output(output)
    if platform == "iosxr":
        match = re.search(r"Success rate is\s+\d+\s+percent\s*\((\d+)/(\d+)\)", output)
        if match and int(match[2]) > 0:
            return int(match[1]) > 0
    else:
        match = re.search(r"sent=(\d+)\s+received=(\d+)\s+packet-loss=", output)
        if match and int(match[1]) > 0:
            return int(match[2]) > 0
    raise ValueError("Unable to determine reachability from native probe output")


def _supported_request(intent, platform: str, plan=None) -> VerifyCommand | None:
    if intent.property == "adjacency" and intent.adjacency.protocol == "bgp":
        adjacency = intent.adjacency
        if plan is None and adjacency.route_reflector_client is not None:
            return None
        if plan is None and adjacency.update_source is not None:
            return None
        command = IOSXR_BGP_COMMAND if platform == "iosxr" else ROUTEROS_BGP_COMMAND
        if plan is not None and platform == "iosxr":
            command = _native_command(
                platform, f"show bgp neighbors {adjacency.remote_address}"
            )
        return VerifyCommand(adjacency.local_node, command, 30)
    if plan is not None:
        if intent.adjacency and intent.adjacency.protocol == "ospf":
            if intent.adjacency.ospf_area not in {None, "0", "0.0.0.0"}:
                return None
            return VerifyCommand(
                intent.adjacency.local_node,
                _native_command(
                    platform,
                    "show ospf neighbor detail"
                    if platform == "iosxr"
                    else "/routing/ospf/neighbor/print detail without-paging",
                ),
                30,
            )
        expected = intent.route_next_hops or intent.bgp_route
        if expected:
            if platform == "iosxr":
                command = (
                    f"show route ipv4 unicast {expected.prefix} detail"
                    if intent.route_next_hops
                    else f"show bgp ipv4 unicast {expected.prefix}"
                )
            else:
                command = f"/routing/route/print detail without-paging where dst-address={expected.prefix} active=yes"
                if intent.bgp_route:
                    command += " bgp=yes"
            return VerifyCommand(expected.node, _native_command(platform, command), 30)
    if (
        intent.property in {"reachability", "isolation"}
        and intent.traffic.protocol == "icmp"
    ):
        if (
            intent.source.kind not in {"node", "endpoint"}
            or intent.destination.kind not in {"node", "endpoint"}
            or not intent.source.address
            or not intent.destination.address
        ):
            return None
        if plan is not None and intent.source.name in {
            node.device_name for node in plan.nodes
        }:
            command = (
                f"ping {intent.destination.address} source {intent.source.address} repeat 3 timeout 2"
                if platform == "iosxr"
                else f"/ping address={intent.destination.address} src-address={intent.source.address} count=3 interval=1s"
            )
            return VerifyCommand(
                intent.source.name, _native_command(platform, command), 15
            )
        return VerifyCommand(
            intent.source.name,
            shlex.join(
                [
                    "ping",
                    "-c",
                    "3",
                    "-W",
                    "2",
                    "-I",
                    intent.source.address,
                    intent.destination.address,
                ]
            ),
            15,
        )
    return None


def _verify_contract(
    runtime, contract: ValidationContract, platform: str, plan=None
) -> ValidationReport:
    verifier = f"{platform}-runtime-v1"
    requests = {
        intent.id: _supported_request(intent, platform, plan)
        for intent in contract.intents
    }
    observations = [request for request in requests.values() if request is not None]
    ospf_identity = {}
    if platform == "routeros" and plan is not None:
        for intent in contract.intents:
            if intent.adjacency and intent.adjacency.protocol == "ospf":
                node = intent.adjacency.local_node
                for table in ("instance", "area"):
                    request = VerifyCommand(
                        node,
                        _native_command(
                            platform,
                            f"/routing/ospf/{table}/print detail without-paging",
                        ),
                        30,
                    )
                    ospf_identity[node, table] = request
        observations.extend(ospf_identity.values())
    collected = collect_verify_commands(runtime, observations)
    parsed_bgp: dict[tuple[str, str], dict[str, _BgpPeer]] = {}
    rows = []
    for intent in contract.intents:
        started = perf_counter()
        request = requests[intent.id]
        evidence = {}
        try:
            if request is None:
                raise NotImplementedError(
                    "Predicate is unsupported by the native runtime verifier"
                )
            result = collected.results[request.node, request.command]
            evidence = {
                "node": request.node,
                "command": request.command,
                "output": result.output,
                "returncode": result.returncode,
            }
            if result.error:
                evidence["provider_error"] = result.error
                raise RuntimeError(result.error)
            if intent.adjacency or intent.route_next_hops or intent.bgp_route:
                if result.returncode != 0:
                    raise RuntimeError(f"state query exited {result.returncode}")
            if intent.route_next_hops:
                expected = intent.route_next_hops
                actual = _route_next_hops(result.output, platform, expected.prefix)
                passed = bool(actual) and actual == set(expected.next_hops)
                evidence.update(
                    observed_next_hops=sorted(actual),
                    expected_next_hops=list(expected.next_hops),
                )
            elif intent.bgp_route:
                expected = intent.bgp_route
                paths = _selected_as_paths(result.output, platform, expected.prefix)
                passed = bool(paths) == expected.present and all(
                    (expected.exit_asn is None or path[:1] == (expected.exit_asn,))
                    and (
                        expected.origin_asn is None
                        or path[-1:] == (expected.origin_asn,)
                    )
                    for path in paths
                )
                evidence.update(selected_as_paths=paths, expected=expected.model_dump())
            elif intent.adjacency and intent.adjacency.protocol == "ospf":
                identity = {}
                if platform == "routeros":
                    for table, key in (("instance", "instances"), ("area", "areas")):
                        extra = ospf_identity[intent.adjacency.local_node, table]
                        observed = collected.results[extra.node, extra.command]
                        if observed.error or observed.returncode != 0:
                            raise RuntimeError(
                                observed.error or f"OSPF {table} query failed"
                            )
                        identity[key] = observed.output
                    evidence.update(identity)
                passed = _ospf_established(
                    result.output, platform, intent.adjacency, plan, **identity
                )
            elif intent.property == "adjacency":
                if result.returncode != 0:
                    evidence["provider_error"] = (
                        f"state query exited {result.returncode}"
                    )
                    raise RuntimeError(evidence["provider_error"])
                cache_key = (request.node, request.command)
                if cache_key not in parsed_bgp:
                    try:
                        _, peers = (
                            (None, dict([_iosxr_neighbor(result.output)]))
                            if platform == "iosxr" and plan is not None
                            else _parse_iosxr_bgp_summary(result.output)
                            if platform == "iosxr"
                            else (None, _parse_routeros_bgp_sessions(result.output))
                        )
                    except ValueError as exc:
                        evidence["provider_error"] = str(exc)
                        raise RuntimeError(str(exc)) from exc
                    parsed_bgp[cache_key] = peers
                adjacency = intent.adjacency
                peer = parsed_bgp[cache_key].get(adjacency.remote_address)
                passed = bool(
                    peer
                    and peer.established
                    and peer.remote_asn == adjacency.remote_asn
                    and (
                        peer.local_asn is None or peer.local_asn == adjacency.local_asn
                    )
                )
                if adjacency.session_type is not None and peer is not None:
                    if peer.local_asn is not None:
                        passed = passed and (
                            (peer.local_asn == peer.remote_asn)
                            == (adjacency.session_type == "ibgp")
                        )
                if plan is not None and peer is not None:
                    if peer.local_asn is None or peer.local_address is None:
                        raise ValueError(
                            "Native BGP session omitted its local identity"
                        )
                    passed = passed and peer.local_address == adjacency.local_address
                    if adjacency.route_reflector_client is not None:
                        if peer.rr_client is None:
                            raise ValueError(
                                "Native BGP session omitted its reflector role"
                            )
                        passed = (
                            passed
                            and peer.rr_client == adjacency.route_reflector_client
                        )
                    if adjacency.update_source is not None:
                        node = next(
                            n
                            for n in plan.nodes
                            if n.device_name == adjacency.local_node
                        )
                        source = (
                            node.loopback
                            if adjacency.update_source == "lo"
                            else next(
                                i.address
                                for i in node.interfaces
                                if i.name == adjacency.update_source
                            )
                        )
                        passed = passed and peer.local_address == source
            elif plan is not None and intent.source.name in {
                node.device_name for node in plan.nodes
            }:
                passed = _native_ping_status(result.output, platform) == (
                    intent.property == "reachability"
                )
            else:
                stats = parse_ping_output(result.output)
                if stats["status"] == "unknown":
                    evidence["provider_error"] = (
                        "unable to determine reachability from probe output"
                    )
                    raise RuntimeError(evidence["provider_error"])
                passed = (stats["status"] == "ok") == (
                    intent.property == "reachability"
                )
            status = "passed" if passed else "failed"
            reason = (
                None
                if passed
                else "Observed behavior differs from the original requirement"
            )
        except NotImplementedError as exc:
            status, reason = "unsupported", str(exc)
        except Exception as exc:
            status, reason = "error", str(exc)
        rows.append(
            ValidationResult(
                intent=intent.id,
                verifier=verifier,
                status=status,
                supported=status != "unsupported",
                evidence=evidence,
                reason=reason,
                duration_ms=(perf_counter() - started) * 1000,
            )
        )
    return ValidationReport.from_results(contract, verifier, rows)


def verify_iosxr_contract(
    runtime, contract: ValidationContract, plan=None
) -> ValidationReport:
    return _verify_contract(runtime, contract, "iosxr", plan)


def verify_routeros_contract(
    runtime, contract: ValidationContract, plan=None
) -> ValidationReport:
    return _verify_contract(runtime, contract, "routeros", plan)
