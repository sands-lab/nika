from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.registry import compatible, get_problem_class
from nika.problems.security.web import WebDoS, WebDoSParams
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.symptom import evaluate_symptom
from tests.support.symptom.probe import _resolve_path

WEB_DOS_SCENARIOS = (
    "dc_clos",
    "campus_lan",
    "enterprise_branch",
    "sdn_l3_clos",
    "p4_dc_fabric",
    "p4_dc_gateway",
)


class _Runtime:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    def get_host_ip(self, host: str, *, with_prefix: bool = False) -> str:
        assert host == "web_2"
        assert not with_prefix
        return "10.0.2.11"

    def exec(self, host: str, command: str, timeout: float = 10) -> str:
        self.commands.append((host, command))
        return ""

    def write_file(self, host: str, path: str, content: str) -> None:
        self.commands.append((host, f"WRITE {path} {len(content)}"))


class _DeterministicWebDoS(WebDoS):
    def __init__(self) -> None:
        super().__init__(None)
        self.runtime = _Runtime()
        self._sample_calls = 0

    def _http_samples(self, params, target_ip):
        self._sample_calls += 1
        if self._sample_calls == 1:
            return {
                "successes": 5,
                "error_rate": 0.0,
                "p95_ms": 10.0,
                "median_ms": 8.0,
            }
        return {
            "successes": 2,
            "error_rate": 0.6,
            "p95_ms": 250.0,
            "median_ms": 220.0,
        }

    def _worker_state(self, params):
        return params.workers, params.workers, "ready"

    def _target_connections(self, host):
        return 500

    def _slow_client_running(self, params):
        return True


def _params(**overrides) -> WebDoSParams:
    values = {
        "host_name": "web_2",
        "attacker_device": "client_4_1",
        "observer_device": "client_1_1",
        "probe_url": "http://10.0.2.11/",
    }
    values.update(overrides)
    return WebDoSParams(**values)


def test_web_dos_artifact_verify_only() -> None:
    problem = _DeterministicWebDoS()
    params = _params()

    problem.inject_fault(params)
    result = problem.verify_fault(params)

    assert result["verified"] is True
    assert result["details"]["attack_ready"] is True
    assert "degradation_ok" not in result["details"]
    attacker_commands = [
        cmd for host, cmd in problem.runtime.commands if host == "client_4_1"
    ]
    assert any(
        command.count("nika_web_dos_worker_") == params.workers
        for command in attacker_commands
    )
    assert any("/nika-dos.bin" in command for command in attacker_commands)
    assert any(
        f"-c {params.concurrency_per_worker}" in command
        for command in attacker_commands
    )


def test_web_dos_evaluate_symptom_degradation() -> None:
    problem = _DeterministicWebDoS()
    params = _params()
    problem.inject_fault(params)

    ok, symptom = evaluate_symptom(
        problem.runtime,
        "web_dos_attack",
        params,
        scenario=None,
        problem=problem,
    )
    assert ok is True
    assert symptom["verified"] is True
    assert symptom["details"]["degradation_ok"] is True


def test_web_dos_rejects_observer_on_attacker() -> None:
    problem = _DeterministicWebDoS()
    params = _params(observer_device="client_4_1")

    try:
        problem.inject_fault(params)
    except ValueError as exc:
        assert "independent" in str(exc)
    else:
        raise AssertionError("expected observer/attacker validation failure")


def test_web_dos_requires_an_independent_http_observer_scenario() -> None:
    assert compatible("web_dos_attack", "p4_dc_gateway")
    assert not compatible("web_dos_attack", "llmd_lab")


def test_web_dos_probe_path_uses_case_observer_and_url() -> None:
    params = WebDoSParams(
        host_name="webserver0_pod0",
        attacker_device="client_0",
        observer_device="dns_pod0",
        probe_url="http://10.0.1.2/small.bin",
    )

    path = _resolve_path("dc_clos", params)

    assert path is not None
    assert path.src_host == "dns_pod0"
    assert path.http_url == "http://10.0.1.2/small.bin"

    enterprise = _resolve_path(
        "enterprise_branch",
        WebDoSParams(
            host_name="hq_srv",
            attacker_device="br1_corp_pc",
            observer_device="hq_corp_pc",
            probe_url="http://10.0.20.2/small.bin",
        ),
    )
    assert enterprise is not None
    assert enterprise.src_host == "hq_corp_pc"


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestWebDoSAttackVerify(IntegrationTestCase):
    """Verify-only path: inject → verify_fault → recover."""

    PROBLEM = "web_dos_attack"

    @pytest.mark.parametrize("scenario", WEB_DOS_SCENARIOS)
    def test_inject_verify_restore(self, scenario: str) -> None:
        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(scenario, ["-s", "s"])
            self._assert_session_ready(session_id, scenario)

            raw_params = resolve_inject_params(self.PROBLEM, scenario, "s", seed=1)
            cls = get_problem_class(self.PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(raw_params)

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify
                assert (verify.get("details") or {}).get("attack_ready") is True

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestWebDoSAttackSymptom(IntegrationTestCase):
    """Symptom path: inject → verify_fault → evaluate_symptom → recover."""

    PROBLEM = "web_dos_attack"

    @pytest.mark.parametrize("scenario", WEB_DOS_SCENARIOS)
    def test_inject_symptom_restore(self, scenario: str) -> None:
        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(scenario, ["-s", "s"])
            self._assert_session_ready(session_id, scenario)

            raw_params = resolve_inject_params(self.PROBLEM, scenario, "s", seed=1)
            cls = get_problem_class(self.PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(raw_params)

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify

                ok, symptom = evaluate_symptom(
                    problem.runtime,
                    self.PROBLEM,
                    parsed,
                    scenario=scenario,
                    problem=problem,
                )
                assert ok is True, symptom
                assert (symptom.get("details") or {}).get("degradation_ok") is True

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
