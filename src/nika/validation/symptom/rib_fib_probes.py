"""Compare endpoint traffic and selected paths across a FIB divergence."""

from nika.problems.base import build_verify_result


def rib_fib_baseline(problem, params):
    state = problem.prepare(params)
    before = problem.snapshot(params)
    ok = (
        before["rib"] == state["route"]
        and before["fib"] == state["route"]["hops"]
        and before["sessions"] == state["sessions"]
        and before["loss_percent"] == 0
    )
    if ok:
        problem._audit_rib_fib = before
    return ok, {"src": state["src"], "dst": state["dst"], "before": before}


def rib_fib_diverged(problem, params):
    before = getattr(problem, "_audit_rib_fib", None)
    if before is None:
        return False, {"error": "no_healthy_rib_fib_baseline"}
    after = problem.snapshot(params)
    state = problem._rib_fib
    artifact = problem.verify_fault(params)
    ok = (
        artifact["verified"]
        and after["rib"] == before["rib"]
        and after["sessions"] == before["sessions"]
        and after["fib"] == [[state["wrong_hop"], state["interface"]]]
        and after["loss_percent"] == 100
    )
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={"before": before, "after": after, "artifact": artifact},
    )
