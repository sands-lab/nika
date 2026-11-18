"""Live validation of a standby key error followed by primary WAN loss."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from nika.problems.registry import get_problem_class
from nika.problems.support.polling import wait_until
from nika.service.kathara.prometheus_api import prometheus_query_for
from nika.service.kathara.syslog_api import syslog_query_for
from nika.validation.symptom.custom import (
    evaluate_custom_baseline,
    evaluate_custom_symptom,
)
from nika.validation.symptom.path_probes import echo
from nika.workflows.benchmark.inject_resolve import resolve_multi_inject_params
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import build_eval_metrics_payload, run_eval_metrics

KEY = "wireguard_peer_key_misconfiguration"
PROBLEMS = [KEY, "link_down"]


def _cli(args, *, timeout=900):
    result = subprocess.run(
        ["uv", "run", "nika", *args], capture_output=True, text=True, timeout=timeout
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def run_latent_backup_e2e(test, profile):
    session_id = None
    evidence = {}
    try:
        session_id = start_net_env(
            "enterprise_branch",
            "s",
            device_profile=profile if profile != "frr" else None,
            telemetry=["syslog", "prometheus"],
            session_tag="test",
        )
        row = test._assert_session_ready(session_id, "enterprise_branch")
        directory = Path(row["session_dir"])
        kwargs = test._scenario_kwargs(session_id)
        params = resolve_multi_inject_params(
            PROBLEMS,
            "enterprise_branch",
            "s",
            seed=42,
            isp_options={"device_profile": profile},
        )
        key = get_problem_class(KEY)("enterprise_branch", **kwargs)
        kp = key.parse_params(params[KEY])
        link = get_problem_class("link_down")("enterprise_branch", **kwargs)
        lp = link.parse_params(params["link_down"])
        runtime = key.runtime
        tunnel = key._target_tunnels(kp)[0]
        site = key.net_env.spec.sites[tunnel.spoke]
        source = next(lan.host_names[0] for lan in site.lans if lan.role == "corp")
        guest = next(lan.host_names[0] for lan in site.lans if lan.role == "guest")
        target = key.net_env.default_probe_path().dst_ip
        public = key.net_env.nat_probe_server()[1]
        api = SimpleNamespace(exec_cmd=runtime.exec)
        metric = "routeros_bgp_peer_up" if profile == "routeros" else "frr_bgp_peer_up"
        query = f'{metric}{{node="{kp.host_name}",neighbor="{tunnel.hub_tunnel_ip}"}}'

        def path(healthy, *, primary_up):
            reading = {}

            def expected():
                nonlocal reading
                reading = echo(runtime, source, target, count=5)
                return reading["received"] == (5 if healthy else 0)

            assert wait_until(expected, 240, interval_s=5), reading
            guest_reading = echo(runtime, guest, public, count=5)
            assert guest_reading["received"] == (5 if primary_up else 0), guest_reading

        ok, baseline = evaluate_custom_baseline(KEY, key, kp)
        assert ok, baseline

        def metric_up():
            evidence["healthy_metric"] = prometheus_query_for(api, query=query)
            return any(row.get("value") == 1.0 for row in evidence["healthy_metric"])

        assert wait_until(metric_up, 120, interval_s=5), evidence.get("healthy_metric")
        started = time.time()
        key.inject_fault(kp)
        assert key.verify_fault(kp)["verified"]
        ok, latent = evaluate_custom_symptom(KEY, key, kp)
        assert ok, latent
        assert len(latent["established"]) == len(baseline["established"]) - 1, latent
        path(True, primary_up=True)
        # Record a bounded pre-primary interval after the exporter has caught up.
        time.sleep(15)
        latent_start = time.time()
        time.sleep(20)
        primary_time = time.time()
        args = ["failure", "inject", *PROBLEMS, "--session_id", session_id]
        for name, fields in params.items():
            for field, value in fields.items():
                args += ["--set", f"{name}.{field}={value}"]
        _cli(args)
        test._assert_multi_failure_injected(PROBLEMS, session_id)
        path(False, primary_up=False)

        def all_sessions_lost():
            ok, reading = evaluate_custom_symptom(KEY, key, kp)
            evidence["both_faults"] = reading
            return ok and not reading["established"]

        assert wait_until(all_sessions_lost, 240, interval_s=5), evidence.get(
            "both_faults"
        )
        control_site = next(
            s for s in key.net_env.spec.branch_names() if s != tunnel.spoke
        )
        control_source = next(
            lan.host_names[0]
            for lan in key.net_env.spec.sites[control_site].lans
            if lan.role == "corp"
        )
        evidence["unaffected_branch"] = echo(runtime, control_source, target, count=5)
        assert evidence["unaffected_branch"]["received"] == 5, evidence[
            "unaffected_branch"
        ]
        evidence["backup_history"] = prometheus_query_for(
            api,
            query=query,
            start_time=str(latent_start),
            end_time=str(primary_time),
            step="5s",
        )
        samples = [
            v for row in evidence["backup_history"] for _, v in row.get("values", [])
        ]
        assert len(samples) >= 3 and set(samples) == {0.0}, evidence["backup_history"]
        evidence["syslog"] = syslog_query_for(
            api,
            start_time=str(started),
            host=kp.host_name,
            contains=tunnel.hub_tunnel_ip,
            limit=100,
        )
        assert evidence["syslog"], "No backup neighbor history in session syslog"
        _cli(
            ["agent", "run", "--agent", "oracle", "--session_id", session_id],
            timeout=1800,
        )
        oracle = json.loads((directory / "oracle.json").read_text())
        assert oracle["solved"] is True, oracle
        metrics, status = build_eval_metrics_payload(
            gt=json.loads((directory / "ground_truth.json").read_text()),
            submission=json.loads((directory / "submission.json").read_text()),
            trace_metrics={},
        )
        assert status == "scored", metrics
        assert metrics["detection_score"] == metrics["rca_accuracy"] == 1.0, metrics

        assert link.recover_fault(lp)["verified"]
        path(True, primary_up=True)
        assert key.verify_fault(kp)["verified"], (
            "Latent fault disappeared with primary recovery"
        )
        link.inject_fault(lp)
        path(False, primary_up=False)
        assert key.recover_fault(kp)["verified"]
        path(True, primary_up=False)
        assert wait_until(metric_up, 120, interval_s=5), evidence.get("healthy_metric")
        assert link.verify_fault(lp)["verified"], (
            "Primary fault disappeared with backup recovery"
        )
        assert link.recover_fault(lp)["verified"]
        path(True, primary_up=True)

        def all_sessions_restored():
            ok, reading = evaluate_custom_baseline(KEY, key, kp)
            evidence["recovered_sessions"] = reading
            return ok and set(baseline["established"]) <= set(reading["established"])

        assert wait_until(all_sessions_restored, 240, interval_s=5), evidence.get(
            "recovered_sessions"
        )
    finally:
        if session_id is not None:
            test._close_session(session_id)

    run_eval_metrics(session_id=session_id)
    metrics = json.loads((directory / "eval_metrics.json").read_text())
    assert metrics["detection_score"] == metrics["rca_accuracy"] == 1.0, metrics
