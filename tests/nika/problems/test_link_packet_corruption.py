"""Docker E2E for link_packet_corruption (VDE/host-veth corrupt + partial degradation)."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.net_env.verify import http_download_stats, iperf_tcp_metrics, ping_stats
from nika.problems.link_interface.link import _resolve_link_intf
from nika.problems.registry import get_problem_class, list_avail_problem_names
from nika.runtime.kathara.runtime import KatharaRuntime
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    privileged_lab_supported,
)
from tests.support.symptom import evaluate_symptom, get_symptom_contract
from tests.support.symptom.flap_probes import assert_baseline_healthy
from tests.support.symptom.probe import _resolve_path

PROBLEM = "link_packet_corruption"
SEEDS = (0, 1, 4, 7)
IPERF_DURATION_SEC = 5
BASELINE_RETRIES = 3
BASELINE_RETRY_SLEEP_SEC = 5.0


@dataclass(frozen=True)
class CorruptionScenarioCase:
    id: str
    scenario: str
    env_args: tuple[str, ...]
    topo_size: str = "s"
    isp_options: dict[str, str] | None = None
    require_clab: bool = False
    require_privileged: bool = False


CORRUPTION_CASES = (
    CorruptionScenarioCase("simple_bgp", "simple_bgp", (), ""),
    CorruptionScenarioCase("dc_clos", "dc_clos", ("-s", "s")),
    CorruptionScenarioCase("campus_lan", "campus_lan", ("-s", "s")),
    CorruptionScenarioCase("enterprise_branch", "enterprise_branch", ("-s", "s")),
    CorruptionScenarioCase("p4_dc_gateway", "p4_dc_gateway", ("-s", "s")),
    CorruptionScenarioCase("k8s_lab", "k8s_lab", (), "", require_privileged=True),
    CorruptionScenarioCase(
        "isp-abilene-ebgp",
        "isp_abilene",
        ("--igp", "ospf", "--bgp-mode", "ebgp"),
        "s",
        isp_options={
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": False,
        },
    ),
    CorruptionScenarioCase(
        "isp-abilene-ebgp-rpki",
        "isp_abilene_ebgp_rpki",
        (),
        "s",
        isp_options=None,
    ),
)


def test_failure_registers_and_contracts() -> None:
    assert PROBLEM in list_avail_problem_names()
    cls = get_problem_class(PROBLEM)
    assert cls is not None
    contract = get_symptom_contract(PROBLEM)
    assert contract.symptom_class == "degradation"
    assert contract.probe == "custom"
    assert "evaluate_symptom" not in cls.__dict__


def _skip_reason(case: CorruptionScenarioCase) -> str | None:
    if not docker_available():
        return "docker required"
    if case.require_clab and not containerlab_prerequisites():
        return "containerlab/gnmic not available"
    if case.require_privileged and not privileged_lab_supported():
        return "privileged k8s lab not supported"
    return None


def _qdisc(runtime, node: str, intf: str) -> str:
    return runtime.exec(node, f"tc qdisc show dev {intf} 2>/dev/null || true").lower()


def _assert_baseline_with_retry(runtime, path) -> tuple[bool, dict]:
    last: dict = {}
    for attempt in range(BASELINE_RETRIES):
        ok, details = assert_baseline_healthy(runtime, path)
        last = details
        if ok:
            return True, details
        if attempt + 1 < BASELINE_RETRIES:
            time.sleep(BASELINE_RETRY_SLEEP_SEC)
    return False, last


def _capture_degradation_baselines(problem, runtime, path) -> None:
    peer_host = path.peer_host or path.src_host
    baseline_bps, baseline_retrans = iperf_tcp_metrics(
        runtime,
        path.src_host,
        peer_host,
        path.dst_ip,
        duration_sec=IPERF_DURATION_SEC,
        port=15231,
    )
    if baseline_bps is not None:
        problem._baseline_iperf_bps = baseline_bps
        problem._baseline_iperf_retrans = baseline_retrans or 0
    if path.http_url:
        http = http_download_stats(
            runtime,
            path.src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        if http.ok and http.time_total_s is not None:
            problem._baseline_http_time_s = http.time_total_s
    ping = ping_stats(runtime, path.src_host, path.dst_ip, count=15, interval_sec=0.2)
    if ping.rtt_avg_ms is not None:
        problem._baseline_rtt_ms = ping.rtt_avg_ms


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("case", CORRUPTION_CASES, ids=[c.id for c in CORRUPTION_CASES])
class TestLinkPacketCorruptionE2E(IntegrationTestCase):
    """Inject → verify_fault → partial degradation symptom → recover."""

    def test_inject_verify_symptom_recover(
        self, case: CorruptionScenarioCase, seed: int
    ) -> None:
        skip = _skip_reason(case)
        if skip:
            pytest.skip(skip)

        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(case.scenario, list(case.env_args))
            self._assert_session_ready(session_id, case.scenario)

            raw = resolve_inject_params(
                PROBLEM,
                case.scenario,
                case.topo_size,
                seed=seed,
                isp_options=case.isp_options,
            )
            cls = get_problem_class(PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(raw)
            runtime = problem.runtime

            path = _resolve_path(case.scenario, parsed, topo_size=case.topo_size or "s")
            assert path is not None and path.dst_ip and path.peer_host

            baseline_ok, baseline = _assert_baseline_with_retry(runtime, path)
            assert baseline_ok is True, baseline

            _capture_degradation_baselines(problem, runtime, path)
            assert (
                getattr(problem, "_baseline_iperf_bps", None) is not None
                or getattr(problem, "_baseline_http_time_s", None) is not None
                or getattr(problem, "_baseline_rtt_ms", None) is not None
            ), "need at least one degradation baseline metric"

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify
                assert verify["details"]["artifact"]["verified"] is True
                assert verify["details"]["symptom"]["verified"] is True

                backend = (
                    "kathara" if isinstance(runtime, KatharaRuntime) else "containerlab"
                )
                resolved_intf = _resolve_link_intf(parsed.intf_name, backend)
                host_qdisc = _qdisc(runtime, parsed.host_name, resolved_intf)
                assert "netem" not in host_qdisc and "tbf" not in host_qdisc, host_qdisc

                if isinstance(runtime, KatharaRuntime):
                    controller = KatharaVdeFaultProxy(runtime)
                    proxy = controller.discover(parsed.host_name, resolved_intf)
                    assert proxy is not None
                    assert controller.netem_corrupt_configured(proxy)
                    peer = (
                        (proxy.peer.node, proxy.peer.intf)
                        if proxy.endpoint.node == parsed.host_name
                        and proxy.endpoint.intf == resolved_intf
                        else (proxy.endpoint.node, proxy.endpoint.intf)
                    )
                    peer_qdisc = _qdisc(runtime, peer[0], peer[1])
                    assert "netem" not in peer_qdisc and "tbf" not in peer_qdisc

                ok, symptom = evaluate_symptom(
                    runtime,
                    PROBLEM,
                    parsed,
                    scenario=case.scenario,
                    topo_size=case.topo_size or "s",
                    problem=problem,
                )
                assert ok is True, symptom
                cmp = symptom.get("comparison", {})
                assert cmp.get("partial_loss") is True, symptom
                assert cmp.get("tcp_degraded") is True, symptom
                assert cmp.get("link_up") is True, symptom

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered

                baseline_bps = getattr(problem, "_baseline_iperf_bps", None)
                if baseline_bps is not None:
                    restored_bps, _ = iperf_tcp_metrics(
                        runtime,
                        path.src_host,
                        path.peer_host,
                        path.dst_ip,
                        duration_sec=IPERF_DURATION_SEC,
                        port=15232,
                    )
                    assert restored_bps is not None
                    assert float(restored_bps) >= float(baseline_bps) * 0.7
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
