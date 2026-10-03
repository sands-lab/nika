"""DHCP client symptom probes (test-path only).

Spoofed-option faults read the configuration the client applied from its
lease and the reachability it implies. Missing-subnet and service-down faults
also send a one-shot DHCPDISCOVER that never applies the offered lease.
"""

from __future__ import annotations

import re
from typing import Any

from nika.net_env.campus_lan.verify import PROBE_HOST
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime

_NAME = "web0.local"
_CLIENT_STATE = (
    "ip -4 -o addr show dev eth0 scope global; echo @route; "
    "ip -4 route show default; echo @dns; cat /etc/resolv.conf"
)
_DISCOVER = (
    "d=$(mktemp -d); printf 'timeout 4;\\n' > $d/c; "
    "timeout 8 dhclient -1 -d -v -cf $d/c -sf /bin/true -lf $d/l -pf $d/p eth0 2>&1; "
    "rm -rf $d"
)


def _client(params: Any) -> str:
    return getattr(params, "host_name_2", None) or PROBE_HOST


def _client_state(runtime: LabRuntime, host: str) -> dict[str, Any] | None:
    out = runtime.exec(host, _CLIENT_STATE, timeout=10)
    if "@route" not in out or "@dns" not in out:
        return None
    addr, rest = out.split("@route", 1)
    route, dns = rest.split("@dns", 1)
    inet = re.search(r"inet (\S+)", addr)
    via = re.search(r"default via (\S+)", route)
    return {
        "address": inet.group(1) if inet else None,
        "gateway": via.group(1) if via else None,
        "dns": re.findall(r"^nameserver\s+(\S+)", dns, re.M),
    }


def _ping(runtime: LabRuntime, host: str, ip: str) -> int | None:
    """Echo replies received out of 2, or ``None`` when ping did not report."""
    out = runtime.exec(host, f"ping -c 2 -W 2 {ip}", timeout=15)
    match = re.search(r"(\d+) received", out)
    if match:
        return int(match.group(1))
    return 0 if "Network is unreachable" in out else None


def _resolve(runtime: LabRuntime, host: str, server: str = "") -> list[str] | None:
    """A answers for ``_NAME``; ``[]`` when resolution failed, ``None`` if unparsed."""
    at = f"@{server} " if server else ""
    out = runtime.exec(host, f"dig {at}+time=2 +tries=1 {_NAME}", timeout=10)
    if "no servers could be reached" in out:
        return []
    if "status: " not in out:
        return None
    return re.findall(rf"^{re.escape(_NAME)}\.\s+\d+\s+IN\s+A\s+(\S+)", out, re.M)


def _discover(runtime: LabRuntime, host: str) -> dict[str, Any] | None:
    """Send one DHCPDISCOVER whose lease is never applied to the interface."""
    out = runtime.exec(host, _DISCOVER, timeout=20)
    offer = re.search(r"DHCPOFFER of (\S+) from (\S+)", out)
    if "DHCPDISCOVER on" not in out or (
        offer is None and "No DHCPOFFERS received" not in out
    ):
        return None
    return {
        "offer": offer.group(1) if offer else None,
        "offer_from": offer.group(2) if offer else None,
    }


def _healthy(problem: Any, params: Any, *, discover: bool) -> tuple[bool, dict]:
    runtime = problem.runtime
    client = _client(params)
    state = _client_state(runtime, client)
    if not state or not state["address"] or not state["gateway"] or not state["dns"]:
        return False, {
            "client": client,
            "error": "client_lease_unreadable",
            "state": state,
        }
    evidence: dict[str, Any] = {
        "client": client,
        "state": state,
        "gateway_replies": _ping(runtime, client, state["gateway"]),
        "dns_server_replies": _ping(runtime, client, state["dns"][0]),
        "resolved": _resolve(runtime, client),
    }
    ok = bool(
        evidence["gateway_replies"]
        and evidence["dns_server_replies"]
        and evidence["resolved"]
    )
    if discover:
        evidence["discover"] = _discover(runtime, client)
        ok = ok and bool(evidence["discover"] and evidence["discover"]["offer"])
    problem._audit_dhcp_baseline = evidence
    return ok, evidence


def dhcp_client_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return _healthy(problem, params, discover=False)


def dhcp_offer_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return _healthy(problem, params, discover=True)


def _result(
    problem: Any, verified: bool, details: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    return verified, build_verify_result(
        fault_type=problem.root_cause_name, verified=verified, details=details
    )


def _observe(problem: Any, params: Any) -> tuple[dict | None, dict]:
    base = getattr(problem, "_audit_dhcp_baseline", None) or {}
    if not base.get("state"):
        return None, {"error": "no_baseline_lease"}
    client = _client(params)
    state = _client_state(problem.runtime, client)
    if state is None:
        return None, {"error": "client_lease_unreadable", "client": client}
    return base, {"client": client, "baseline_state": base["state"], "state": state}


def dhcp_spoofed_gateway(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The client routes via the leased gateway and loses off-subnet hosts."""
    base, details = _observe(problem, params)
    if base is None:
        return False, details
    runtime, client = problem.runtime, details["client"]
    remote = _ping(runtime, client, base["state"]["dns"][0])
    control = _ping(runtime, client, base["state"]["gateway"])
    if remote is None or control is None:
        return False, {**details, "error": "ping_unparsed"}
    gateway = details["state"]["gateway"]
    changed = gateway is not None and gateway != base["state"]["gateway"]
    control_ok = control > 0
    return _result(
        problem,
        changed and remote == 0 and control_ok,
        {
            **details,
            "gateway_changed": changed,
            "remote_replies": remote,
            "baseline_gateway_replies": control,
            "control_ok": control_ok,
        },
    )


def dhcp_spoofed_dns(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The client queries the leased resolver and names stop resolving."""
    base, details = _observe(problem, params)
    if base is None:
        return False, details
    runtime, client = problem.runtime, details["client"]
    resolved = _resolve(runtime, client)
    control = _resolve(runtime, client, base["state"]["dns"][0])
    if resolved is None or control is None:
        return False, {**details, "error": "dig_unparsed"}
    changed = details["state"]["dns"] != base["state"]["dns"]
    control_ok = control == base["resolved"]
    return _result(
        problem,
        changed and not resolved and control_ok,
        {
            **details,
            "resolver_changed": changed,
            "resolved": resolved,
            "baseline_resolver_answers": control,
            "control_ok": control_ok,
        },
    )


def dhcp_spoofed_subnet(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The leased prefix no longer covers the gateway, so it is unreachable."""
    base, details = _observe(problem, params)
    if base is None:
        return False, details
    gateway = _ping(problem.runtime, details["client"], base["state"]["gateway"])
    if gateway is None:
        return False, {**details, "error": "ping_unparsed"}
    address = details["state"]["address"]
    changed = address is not None and address != base["state"]["address"]
    return _result(
        problem,
        changed and gateway == 0,
        {**details, "address_changed": changed, "gateway_replies": gateway},
    )


def _no_offer(problem: Any, params: Any, details: dict) -> tuple[bool, dict]:
    relay_ip = (problem._audit_dhcp_baseline.get("discover") or {}).get("offer_from")
    if not relay_ip:
        return False, {**details, "error": "no_baseline_offer"}
    discover = _discover(problem.runtime, details["client"])
    relay = _ping(problem.runtime, params.host_name, relay_ip)
    if discover is None or relay is None:
        return False, {**details, "error": "discover_or_ping_unparsed"}
    control_ok = relay > 0
    return discover["offer"] is None and control_ok, {
        **details,
        "discover": discover,
        "server_to_relay_replies": relay,
        "control_ok": control_ok,
    }


def dhcp_missing_subnet(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The client segment gets no offer, holds no address and loses its gateway."""
    base, details = _observe(problem, params)
    if base is None:
        return False, details
    no_offer, details = _no_offer(problem, params, details)
    if "error" in details:
        return False, details
    gateway = _ping(problem.runtime, details["client"], base["state"]["gateway"])
    if gateway is None:
        return False, {**details, "error": "ping_unparsed"}
    unleased = details["state"]["address"] is None
    return _result(
        problem,
        no_offer and unleased and gateway == 0,
        {**details, "unleased": unleased, "gateway_replies": gateway},
    )


def dhcp_service_down(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """A fresh DHCPDISCOVER gets no offer while the server host stays reachable."""
    base, details = _observe(problem, params)
    if base is None:
        return False, details
    no_offer, details = _no_offer(problem, params, details)
    if "error" in details:
        return False, details
    return _result(problem, no_offer, details)
