"""Business-path latency compared with an unaffected network probe."""

from nika.net_env.verify import ping_stats
from nika.validation.symptom.corruption_probes import _ends
from nika.problems.link_interface.link import _link_peer


def _reading(problem, ends, control):
    affected = ping_stats(problem.runtime, ends["src"], ends["dst"], count=6)
    unaffected = ping_stats(problem.runtime, control["src"], control["dst"], count=6)
    return {
        "rtt_ms": affected.rtt_avg_ms,
        "loss_percent": affected.loss_percent,
        "control_rtt_ms": unaffected.rtt_avg_ms,
        "control_loss_percent": unaffected.loss_percent,
    }


def latency_baseline(problem, params):
    ends = _ends(problem, params)
    if "error" in ends:
        return False, ends
    peer = _link_peer(problem.net_env, params.host_name, problem._intf(params))
    attached = {params.host_name, peer[0] if peer else ""}
    # Use the gateway of the endpoint off the delayed access link.
    control_src = ends["dst_host"] if ends["src"] in attached else ends["src"]
    route = problem.runtime.exec(control_src, "ip -4 route show default").split()
    if "via" not in route:
        return False, {"error": "no_control_gateway"}
    control = {"src": control_src, "dst": route[route.index("via") + 1]}
    reading = _reading(problem, ends, control)
    problem._audit_latency = {"ends": ends, "control": control, "before": reading}
    return all(
        reading[key] == 0 for key in ("loss_percent", "control_loss_percent")
    ) and all(
        reading[key] is not None for key in ("rtt_ms", "control_rtt_ms")
    ), problem._audit_latency


def link_delayed(problem, params):
    state = getattr(problem, "_audit_latency", None)
    if not state:
        return False, {"error": "no_latency_baseline"}
    after = _reading(problem, state["ends"], state["control"])
    before = state["before"]
    ok = (
        after["rtt_ms"] is not None
        and after["control_rtt_ms"] is not None
        and after["loss_percent"] == after["control_loss_percent"] == 0
        and after["rtt_ms"] - before["rtt_ms"] >= params.delay_ms * 0.7
        and after["control_rtt_ms"] - before["control_rtt_ms"]
        < max(10, params.delay_ms * 0.25)
    )
    return ok, {**state, "after": after}
