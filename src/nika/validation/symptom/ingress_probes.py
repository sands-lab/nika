"""Observe partial ingress rate rejection and directly reachable backends."""

from __future__ import annotations

import json
import shlex

from nika.problems.base import build_verify_result
from nika.service.lab.k8s_api import K8sCommandError
from nika.validation.symptom.k8s_probes import (
    _ingress_class_sample,
    _running_pods,
    _wait_for,
)


def _rate_sample(problem, *, limited=False):
    obj, (client, url, rate) = problem.select_target()
    limit = max(1, int(rate / 4))
    count = 4 * (5 * limit + 1)
    # Wait for one admitted request before the burst, even if business traffic
    # has already drained the bucket. Keep the same request count in baseline.
    script = f"""
import collections, concurrent.futures, json, time, urllib.request, urllib.error
url = {url!r}
def request(_):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception:
        return 0
first = request(0)
deadline = time.monotonic() + 70
while {limited!r} and first == 503 and time.monotonic() < deadline:
    time.sleep(1)
    first = request(0)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    codes = [first, *pool.map(request, range({count - 1}))]
print(json.dumps(dict(collections.Counter(codes))))
"""
    try:
        output = problem.runtime.exec(
            client, f"python3 -c {shlex.quote(script)}", timeout=150
        )
        codes = json.loads(output.strip())
    except Exception as exc:  # noqa: BLE001 - transport or invalid probe output
        raise K8sCommandError(f"Cannot read ingress request results: {exc}") from exc
    if not isinstance(codes, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in codes.items()
    ):
        raise K8sCommandError(f"Invalid ingress request results: {codes!r}")
    health = _ingress_class_sample(problem)["backends"]
    namespaces = {ref.split("/", 1)[0] for ref in health}
    pods = [
        pod
        for pod in _running_pods(problem.runtime.lab_api, problem.control_node())
        if pod["namespace"] in namespaces
    ]
    return {
        "url": url,
        "client": client,
        "codes": codes,
        "requests": count,
        "backends": health,
        "pods_ready": bool(pods) and all(pod["ready"] for pod in pods),
    }


def ingress_rate_baseline(problem, params):
    """The same burst fully succeeds and every backend has reachable endpoints."""

    def check():
        sample = _rate_sample(problem)
        ok = (
            sample["codes"] == {"200": sample["requests"]}
            and sample["pods_ready"]
            and bool(sample["backends"])
            and all(
                backend["endpoints"] and backend["reachable"] is True
                for backend in sample["backends"].values()
            )
        )
        return ok, sample

    ok, sample = _wait_for(check)
    if ok:
        problem._audit_ingress_rate = sample
    return ok, sample


def ingress_rate_limited(problem, params):
    """A burst has successes and at least 30% HTTP 503 with healthy backends."""
    before = getattr(problem, "_audit_ingress_rate", {})

    def check():
        sample = _rate_sample(problem, limited=True)
        codes = sample["codes"]
        logs = problem.runtime.lab_api.kubectl(
            problem.control_node(params),
            "logs -n ingress-nginx -l app.kubernetes.io/component=controller --since=120s --tail=1000",
        ).stdout
        lines = [
            line
            for line in logs.splitlines()
            if "limiting requests" in line
            and sample["url"].split("http://", 1)[1].split("/", 1)[1] in line
        ]
        share = codes.get("503", 0) / sample["requests"]
        ok = (
            codes.get("200", 0) > 0
            and share >= 0.3
            and sum(codes.values()) == sample["requests"]
            and set(codes) <= {"200", "503"}
            and bool(lines)
            and sample["pods_ready"]
            and bool(before.get("backends"))
            and sample["backends"] == before["backends"]
        )
        return ok, {**sample, "failure_share": share, "limiting_log_lines": len(lines)}

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )
