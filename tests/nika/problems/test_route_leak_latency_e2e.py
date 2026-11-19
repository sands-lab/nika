"""Live route-leak and cable-delay workflows, including Oracle and recovery."""

import fcntl
import json
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from nika.problems.support.polling import wait_until
from nika.utils.session import Session
from nika.validation.symptom.custom import (
    evaluate_custom_baseline,
    evaluate_custom_symptom,
)
from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.workflows.failure.inject import prepare_failure, inject_prepared_failure
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available, containerlab_prerequisites


@pytest.mark.e2e
@pytest.mark.parametrize(
    "scenario,backend,failure",
    [
        ("isp_abilene", "kathara", "bgp_route_leak"),
        ("isp_dfn-bwin", "kathara", "bgp_route_leak"),
        ("dc_clos", "kathara", "link_latency"),
        ("dc_clos", "containerlab", "link_latency"),
    ],
)
class TestRouteLeakLatency(IntegrationTestCase):
    def test_workflow(self, scenario, backend, failure):
        if not docker_available():
            pytest.skip("Docker required")
        if backend == "containerlab" and not containerlab_prerequisites():
            pytest.skip("Containerlab and gnmic required")
        # All Containerlab labs share the host's heavy-lab lock.
        lock = (
            open("/tmp/nika-heavy-lab.lock", "a")
            if backend == "containerlab"
            else nullcontext()
        )
        with lock as handle:
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            self._exercise(scenario, backend, failure)

    def _exercise(self, scenario, backend, failure):
        sid = None
        prepared = None
        options = {"backend": backend}
        args = ["--backend", backend]
        if failure == "bgp_route_leak":
            options.update(igp="ospf", bgp_mode="ebgp")
            args += ["--igp", "ospf", "--bgp-mode", "ebgp"]
        else:
            args += ["-s", "s"]
        try:
            sid = self._start_env(scenario, args)
            print("SESSION", sid, flush=True)
            session = Session().load_running_session(sid)
            raw = resolve_inject_params(
                failure, scenario, "s", seed=1, isp_options=options
            )
            prepared = prepare_failure(session, [failure], raw)
            problem, params = prepared.problem, prepared.params
            before_cfg = (
                problem.runtime.exec(params.host_name, "vtysh -c 'show running-config'")
                if failure == "bgp_route_leak"
                else None
            )
            baseline = evaluate_custom_baseline(failure, problem, params)
            print("BASELINE", json.dumps(baseline), flush=True)
            assert baseline[0], baseline
            verify = inject_prepared_failure(
                session, [failure], prepared, param_overrides=raw
            )
            print("VERIFY", json.dumps(verify), flush=True)
            assert wait_until(
                lambda: evaluate_custom_symptom(failure, problem, params)[0],
                90,
                interval_s=3,
            )
            symptom = evaluate_custom_symptom(failure, problem, params)
            print("SYMPTOM", json.dumps(symptom), flush=True)
            assert symptom[0], symptom
            if failure == "bgp_route_leak":
                node, source_ip, destination = problem.business_flow()
                command = f"vtysh -c 'show bgp ipv4 unicast 198.51.100.0/24 json'; traceroute -n -s {source_ip} -q 1 -w 1 -m 32 {destination}"
            else:
                node = params.observer_device or params.host_name
                command = f"ping -n -c 4 {params.probe_dst_ip}; tc qdisc show"
                for lab_node in problem.runtime.list_nodes():
                    qdisc = problem.runtime.exec(
                        lab_node, "tc qdisc show 2>/dev/null || true"
                    )
                    assert "netem" not in qdisc, (lab_node, qdisc)
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "nika",
                    "exec",
                    "--session_id",
                    sid,
                    "--timeout",
                    "50",
                    node,
                    command,
                ],
                text=True,
                capture_output=True,
            )
            print("NIKA EXEC", result.stdout, result.stderr, flush=True)
            assert result.returncode == 0
            self._run_agent(agent_type="oracle", session_id=sid)
            oracle = json.loads((Path(session.session_dir) / "oracle.json").read_text())
            print("ORACLE", json.dumps(oracle), flush=True)
            assert all(row["confirmed"] for row in oracle["faults"]), oracle
            expected_config = None
            if before_cfg is not None:
                current = problem._config(params.host_name)
                peer = problem.session(params)["neighbor"]
                assert problem._original_export == problem._binding(before_cfg, peer)
                expected_config = current.replace(
                    f"neighbor {peer} route-map {problem._ordinary_export} out",
                    f"neighbor {peer} route-map {problem._original_export} out",
                )
            recovered = prepared.recover()
            print("RECOVER", json.dumps(recovered), flush=True)
            assert recovered["verified"], recovered
            assert prepared.recover()["verified"]
            if expected_config is not None:
                assert problem._config(params.host_name) == expected_config
            assert wait_until(
                lambda: not evaluate_custom_symptom(failure, problem, params)[0],
                90,
                interval_s=3,
            )
            healthy = evaluate_custom_baseline(failure, problem, params)
            print("HEALTHY", json.dumps(healthy), flush=True)
            assert healthy[0], healthy
        finally:
            try:
                if prepared is not None:
                    prepared.recover()
            finally:
                if sid is not None:
                    result = subprocess.run(
                        [
                            "uv",
                            "run",
                            "nika",
                            "session",
                            "close",
                            "--session_id",
                            sid,
                            "-y",
                        ],
                        text=True,
                        capture_output=True,
                    )
                    print(
                        "CLOSE",
                        sid,
                        result.returncode,
                        result.stdout,
                        result.stderr,
                        flush=True,
                    )
                    assert result.returncode == 0
