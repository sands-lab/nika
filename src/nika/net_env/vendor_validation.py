"""Evaluate original validation contracts on IOS XR and RouterOS runtimes."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
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
    as_columns = [i for i, field in enumerate(header or ()) if field.lower() == "as"]
    if not as_columns:
        raise ValueError("IOS XR returned no recognizable BGP summary header")
    local_asn = int(local_match.group(1))
    remote_as_index = as_columns[0]
    peers: dict[str, _BgpPeer] = {}
    for line in output.splitlines():
        fields = line.split()
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
        # This command filters to established sessions. RouterOS versions may
        # include or omit the E flag in terse output.
        peers[address_match.group(1)] = _BgpPeer(
            int(remote_as_match.group(1)) if remote_as_match else None,
            True,
            int(local_as_match.group(1)) if local_as_match else None,
        )
    if (
        output.strip()
        and not peers
        and not re.search(r"(?:^|\n)\s*(?:Flags:|Columns:|#\s+)", output, re.I)
    ):
        raise ValueError("RouterOS returned unrecognized BGP session output")
    return peers


def _supported_request(intent, platform: str) -> VerifyCommand | None:
    if intent.property == "adjacency" and intent.adjacency.protocol == "bgp":
        adjacency = intent.adjacency
        if adjacency.route_reflector_client is not None:
            return None
        if adjacency.update_source is not None:
            return None
        command = IOSXR_BGP_COMMAND if platform == "iosxr" else ROUTEROS_BGP_COMMAND
        return VerifyCommand(adjacency.local_node, command, 30)
    if (
        intent.property in {"reachability", "isolation"}
        and intent.traffic.protocol == "icmp"
    ):
        if (
            intent.source.kind != "node"
            or intent.destination.kind != "node"
            or not intent.source.address
            or not intent.destination.address
        ):
            return None
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
    runtime, contract: ValidationContract, platform: str
) -> ValidationReport:
    verifier = f"{platform}-runtime"
    requests = {
        intent.id: _supported_request(intent, platform) for intent in contract.intents
    }
    collected = collect_verify_commands(
        runtime, [request for request in requests.values() if request is not None]
    )
    parsed_bgp: dict[str, dict[str, _BgpPeer]] = {}
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
            if intent.property == "adjacency":
                if result.returncode != 0:
                    evidence["provider_error"] = (
                        f"state query exited {result.returncode}"
                    )
                    raise RuntimeError(evidence["provider_error"])
                if request.node not in parsed_bgp:
                    try:
                        _, peers = (
                            _parse_iosxr_bgp_summary(result.output)
                            if platform == "iosxr"
                            else (None, _parse_routeros_bgp_sessions(result.output))
                        )
                    except ValueError as exc:
                        evidence["provider_error"] = str(exc)
                        raise RuntimeError(str(exc)) from exc
                    parsed_bgp[request.node] = peers
                adjacency = intent.adjacency
                peer = parsed_bgp[request.node].get(adjacency.remote_address)
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
    del plan
    return _verify_contract(runtime, contract, "iosxr")


def verify_routeros_contract(
    runtime, contract: ValidationContract, plan=None
) -> ValidationReport:
    del plan
    return _verify_contract(runtime, contract, "routeros")
