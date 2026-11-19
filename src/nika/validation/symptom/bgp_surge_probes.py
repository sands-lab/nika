"""Control-plane symptoms of low headroom and excess BGP advertisements."""

from nika.problems.support.polling import wait_until
from nika.validation.symptom.routing_probes import _customer_reach


def prefix_session_baseline(problem, params):
    receiver, _, neighbor = problem._resolve_session(params)
    reading = {}

    def healthy():
        up = problem._session_established(receiver, neighbor)
        count = problem._received_prefix_count(receiver, neighbor) if up else 0
        reading.update(
            session_established=up,
            received_prefixes=count,
            customer_reach=_customer_reach(problem, params),
        )
        return (
            up
            and count > 0
            and bool(reading["customer_reach"])
            and reading["customer_reach"]["received"] > 0
        )

    ok = wait_until(healthy, 180, interval_s=2)
    problem._prefix_baseline = dict(reading)
    return ok, reading


def low_prefix_headroom(problem, params):
    receiver, _, neighbor = problem._resolve_session(params)
    up = problem._session_established(receiver, neighbor)
    count = problem._received_prefix_count(receiver, neighbor) if up else 0
    limit = problem.prefix_limit(receiver, neighbor)
    details = {
        "session_established": up,
        "received_prefixes": count,
        "maximum_prefix": limit,
    }
    return up and limit is not None and 0 <= limit - count <= 1, details


def prefix_tables_grow(problem, params):
    receiver, _, neighbor = problem._resolve_session(params)
    baseline = getattr(problem, "_prefix_baseline", {})
    if not baseline:
        return False, {"error": "No healthy prefix-count baseline is available."}
    reading = {}

    def grown():
        up = problem._session_established(receiver, neighbor)
        count = problem._received_prefix_count(receiver, neighbor) if up else 0
        routes = problem._flood_prefixes(
            params.flood_count, peer=params.peer_name, neighbor_ip=neighbor
        )
        installed = [p for p in routes if problem._has_route(receiver, p)]
        reading.update(
            session_established=up,
            received_prefixes=count,
            baseline_prefixes=baseline["received_prefixes"],
            installed_additional_prefixes=installed,
        )
        return (
            up
            and count >= baseline["received_prefixes"] + len(routes)
            and len(installed) == len(routes)
        )

    ok = wait_until(grown, 90, interval_s=2)
    return ok, reading
