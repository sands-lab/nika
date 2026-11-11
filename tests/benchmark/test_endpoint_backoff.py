from unittest.mock import Mock

import pytest

from nika.run_config.schema import BenchmarkSettings
from nika.workflows.benchmark import endpoint

pytestmark = pytest.mark.unit


def test_only_endpoint_failures_trip_guard():
    guard = endpoint.EndpointBackoff(BenchmarkSettings(), Mock())
    for outcome in ["endpoint_failed", "infra_failed", "endpoint_failed"]:
        guard.record(outcome)
    assert not guard.paused
    guard.record("endpoint_failed")
    assert guard.paused
    guard.record("agent_failed")
    assert not guard.paused
    guard.record("endpoint_failed")
    guard.record("success")
    assert guard.failures == 0


def test_recovery_backoff_and_timeout(monkeypatch):
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(endpoint.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(endpoint.time, "sleep", sleep)
    settings = BenchmarkSettings(
        endpoint_failure_threshold=1,
        endpoint_backoff_initial_sec=2,
        endpoint_backoff_max_sec=3,
        endpoint_wait_timeout_sec=10,
    )
    probe = Mock(side_effect=[RuntimeError(), None])
    guard = endpoint.EndpointBackoff(settings, probe)
    guard.record("endpoint_failed")
    guard.wait()
    assert sleeps == [2, 3]
    assert not guard.paused
    guard.probe = Mock(side_effect=RuntimeError())
    guard.record("endpoint_failed")
    with pytest.raises(endpoint.EndpointUnavailable):
        guard.wait()
    assert sum(sleeps) == 15
    assert all(call.args[0] <= 10 for call in guard.probe.call_args_list)


@pytest.mark.parametrize("workers", [1, 2])
def test_scheduler_waits_before_admitting_more_trials(monkeypatch, tmp_path, workers):
    from nika.workflows.benchmark import run
    from tests.benchmark.test_admit import _trial

    events = []
    trials = [_trial(scenario="dc_clos", trial_index=i) for i in range(1, 6)]
    guard = endpoint.EndpointBackoff(
        BenchmarkSettings(endpoint_backoff_initial_sec=0.001),
        lambda timeout: events.append("probe"),
    )
    monkeypatch.setattr(
        run,
        "_run_trial_with_timeout",
        lambda trial, **kw: events.append(trial.trial_index),
    )
    monkeypatch.setattr(run, "trial_outcome", lambda _: "endpoint_failed")
    failures = run._run_trials_batch(
        trials,
        continue_on_error=True,
        case_timeout=0,
        agent_type="mock",
        llm_provider="custom",
        model="test",
        max_steps=None,
        result_dir=str(tmp_path),
        session_tag=None,
        release_meta=None,
        max_workers=workers,
        endpoint_guard=guard,
    )
    assert not failures
    assert events.index("probe") < events.index(5)


@pytest.mark.parametrize("workers", [1, 2])
def test_wait_limit_leaves_later_trials_unstarted(monkeypatch, tmp_path, workers):
    from nika.workflows.benchmark import run
    from tests.benchmark.test_admit import _trial

    started = []
    trials = [_trial(scenario="dc_clos", trial_index=i) for i in range(1, 8)]

    def wait(self):
        if self.paused:
            raise endpoint.EndpointUnavailable("wait limit")

    monkeypatch.setattr(endpoint.EndpointBackoff, "wait", wait)
    monkeypatch.setattr(
        run,
        "_run_trial_with_timeout",
        lambda trial, **kw: started.append(trial.trial_index),
    )
    monkeypatch.setattr(run, "trial_outcome", lambda _: "endpoint_failed")
    with pytest.raises(endpoint.EndpointUnavailable):
        run._run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="mock",
            llm_provider="custom",
            model="test",
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=workers,
        )
    assert 7 not in started
    assert len(started) <= 3 + workers - 1


def test_wait_cap_starts_when_threshold_is_crossed(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(endpoint.time, "monotonic", lambda: clock[0])
    probe = Mock()
    guard = endpoint.EndpointBackoff(
        BenchmarkSettings(endpoint_failure_threshold=1, endpoint_wait_timeout_sec=10),
        probe,
    )
    guard.record("endpoint_failed")
    clock[0] = 20.0
    with pytest.raises(endpoint.EndpointUnavailable):
        guard.wait()
    probe.assert_not_called()


def test_scheduler_enforces_cap_with_a_trial_still_running(monkeypatch, tmp_path):
    import threading
    import time
    from nika.workflows.benchmark import run
    from tests.benchmark.test_admit import _trial

    release = threading.Event()
    started = []

    def execute(trial, **kwargs):
        started.append(trial.trial_index)
        if trial.trial_index == 2:
            release.wait(3)

    monkeypatch.setattr(run, "_run_trial_with_timeout", execute)
    monkeypatch.setattr(run, "trial_outcome", lambda _: "endpoint_failed")
    guard = endpoint.EndpointBackoff(
        BenchmarkSettings(endpoint_failure_threshold=1, endpoint_wait_timeout_sec=0.1),
        Mock(side_effect=RuntimeError()),
    )
    before = time.monotonic()
    try:
        with pytest.raises(endpoint.EndpointUnavailable):
            run._run_trials_batch(
                [_trial(scenario="dc_clos", trial_index=i) for i in (1, 2, 3)],
                continue_on_error=True,
                case_timeout=0,
                agent_type="mock",
                llm_provider="custom",
                model="test",
                max_steps=None,
                result_dir=str(tmp_path),
                session_tag=None,
                release_meta=None,
                max_workers=2,
                endpoint_guard=guard,
            )
        assert time.monotonic() - before < 1
        assert 3 not in started
    finally:
        release.set()


@pytest.mark.parametrize("workers", [1, 2])
def test_callback_failure_does_not_count_the_same_trial_twice(
    monkeypatch, tmp_path, workers
):
    from nika.workflows.benchmark import run
    from tests.benchmark.test_admit import _trial

    guard = Mock(paused=False)
    callback = Mock(side_effect=RuntimeError("progress write failed"))
    monkeypatch.setattr(run, "_run_trial_with_timeout", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "trial_outcome", lambda _: "endpoint_failed")
    with pytest.raises(RuntimeError, match="progress write failed"):
        run._run_trials_batch(
            [_trial(scenario="dc_clos")],
            continue_on_error=True,
            case_timeout=0,
            agent_type="mock",
            llm_provider="custom",
            model="test",
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=workers,
            endpoint_guard=guard,
            on_trial_finished=callback,
        )
    guard.record.assert_called_once_with("endpoint_failed")
    callback.assert_called_once()
