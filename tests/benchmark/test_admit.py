"""Unit tests for benchmark resource-class admission."""

from __future__ import annotations

import pytest

import threading
import time
from pathlib import Path
from unittest.mock import patch

from nika.workflows.benchmark.admit import (
    CLASS_CLAB,
    CLASS_K8S,
    CLASS_LARGE,
    CLASS_LIGHT,
    can_admit,
    class_limits,
    pick_admissible,
    resource_class,
    resource_class_for_row,
)
from nika.workflows.benchmark.trials import Trial

pytestmark = pytest.mark.unit


def _trial(
    *,
    scenario: str,
    problem: str = "x",
    trial_index: int = 1,
    case_index: int = 0,
    backend: str | None = None,
    case_key: str | None = None,
) -> Trial:
    row: dict = {"scenario": scenario, "problem": problem, "inject": {}}
    if backend is not None:
        row["backend"] = backend
    key = case_key or f"{scenario}__{problem}"
    return Trial(
        case_index=case_index,
        trial_index=trial_index,
        row=row,
        case_key=key,
        trial_id=f"{key}__t{trial_index:02d}",
    )


def test_resource_class_containerlab_backend() -> None:
    assert resource_class_for_row(
        {"scenario": "isp_pdh", "backend": "containerlab"}
    ) == (CLASS_CLAB)


def test_resource_class_min3clos_without_backend() -> None:
    assert resource_class_for_row({"scenario": "min3clos"}) == CLASS_CLAB


def test_resource_class_from_case_key_token() -> None:
    trial = _trial(
        scenario="isp_pdh",
        case_key=(
            "isp_pdh__bgp_acl_block__s__isis__ibgp_rr__containerlab__"
            "nokia_srlinux__host_name-n1"
        ),
    )
    assert resource_class(trial) == CLASS_CLAB


def test_resource_class_k8s_family() -> None:
    assert resource_class_for_row({"scenario": "k8s_lab"}) == CLASS_K8S
    assert resource_class_for_row({"scenario": "llmd_lab"}) == CLASS_K8S
    assert resource_class_for_row({"scenario": "iosxr_simple_bgp"}) == CLASS_K8S


def test_resource_class_light() -> None:
    assert resource_class_for_row({"scenario": "dc_clos"}) == CLASS_LIGHT
    assert resource_class_for_row({"scenario": "dc_clos", "topo_size": "m"}) == (
        CLASS_LIGHT
    )
    assert (
        resource_class_for_row({"scenario": "isp_pdh", "backend": "kathara"})
        == CLASS_LIGHT
    )


def test_resource_class_large_topo() -> None:
    assert resource_class_for_row({"scenario": "dc_clos", "topo_size": "l"}) == (
        CLASS_LARGE
    )
    assert resource_class_for_row({"scenario": "campus_lan", "topo": "l"}) == (
        CLASS_LARGE
    )


def test_class_limits_serialize_heavy() -> None:
    limits = class_limits(batch_size=4, serialize_heavy=True)
    assert limits[CLASS_CLAB] == 1
    assert limits[CLASS_K8S] == 1
    assert limits[CLASS_LARGE] == 1
    assert limits[CLASS_LIGHT] == 4


def test_class_limits_flat() -> None:
    limits = class_limits(batch_size=3, serialize_heavy=False)
    assert limits == {
        CLASS_CLAB: 3,
        CLASS_K8S: 3,
        CLASS_LARGE: 3,
        CLASS_LIGHT: 3,
    }


def test_pick_admissible_skips_blocked_head() -> None:
    clab_b = _trial(scenario="min3clos", trial_index=2, case_index=1)
    light = _trial(scenario="dc_clos", trial_index=1, case_index=2)
    pending = [clab_b, light]
    limits = class_limits(batch_size=2, serialize_heavy=True)
    in_flight = {CLASS_CLAB: 1, CLASS_K8S: 0, CLASS_LIGHT: 0}
    # Exclusive clab in flight: neither second clab nor light may start.
    assert pick_admissible(pending, in_flight=in_flight, limits=limits) is None


def test_k8s_exclusive_blocks_peers() -> None:
    limits = class_limits(batch_size=4, serialize_heavy=True)
    assert can_admit(CLASS_K8S, in_flight={}, limits=limits)
    assert not can_admit(
        CLASS_LIGHT,
        in_flight={CLASS_K8S: 1},
        limits=limits,
    )
    assert not can_admit(
        CLASS_CLAB,
        in_flight={CLASS_K8S: 1},
        limits=limits,
    )
    assert not can_admit(
        CLASS_K8S,
        in_flight={CLASS_LIGHT: 1},
        limits=limits,
    )


def test_clab_exclusive_blocks_peers() -> None:
    limits = class_limits(batch_size=4, serialize_heavy=True)
    assert can_admit(CLASS_CLAB, in_flight={}, limits=limits)
    assert not can_admit(CLASS_LIGHT, in_flight={CLASS_CLAB: 1}, limits=limits)
    assert not can_admit(CLASS_K8S, in_flight={CLASS_CLAB: 1}, limits=limits)
    assert not can_admit(CLASS_CLAB, in_flight={CLASS_LIGHT: 2}, limits=limits)


def test_large_exclusive_blocks_peers() -> None:
    limits = class_limits(batch_size=4, serialize_heavy=True)
    assert can_admit(CLASS_LARGE, in_flight={}, limits=limits)
    assert not can_admit(CLASS_LIGHT, in_flight={CLASS_LARGE: 1}, limits=limits)
    assert not can_admit(CLASS_LARGE, in_flight={CLASS_LIGHT: 1}, limits=limits)


def test_flat_limits_run_heavy_classes_concurrently() -> None:
    limits = class_limits(batch_size=3, serialize_heavy=False)
    assert can_admit(CLASS_K8S, in_flight={CLASS_K8S: 2}, limits=limits)
    assert not can_admit(CLASS_K8S, in_flight={CLASS_K8S: 3}, limits=limits)
    assert can_admit(CLASS_LIGHT, in_flight={CLASS_CLAB: 1}, limits=limits)
    assert can_admit(CLASS_LARGE, in_flight={CLASS_LIGHT: 1}, limits=limits)
    k8s_a = _trial(scenario="llmd_lab", trial_index=1, case_index=0)
    k8s_b = _trial(scenario="llmd_lab", trial_index=1, case_index=1)
    assert pick_admissible([k8s_a, k8s_b], in_flight={CLASS_K8S: 1}, limits=limits) == 0


def test_pick_admissible_drains_host_for_blocked_exclusive() -> None:
    light_a = _trial(scenario="dc_clos", trial_index=1, case_index=0)
    k8s = _trial(scenario="k8s_lab", trial_index=1, case_index=1)
    light_b = _trial(scenario="dc_clos", trial_index=1, case_index=2)
    limits = class_limits(batch_size=2, serialize_heavy=True)
    # Light ahead of the exclusive trial still starts.
    assert pick_admissible([light_a, k8s, light_b], in_flight={}, limits=limits) == 0
    # Exclusive head waits for peers: stop admitting lights behind it.
    in_flight = {CLASS_LIGHT: 1}
    assert pick_admissible([k8s, light_b], in_flight=in_flight, limits=limits) is None
    assert pick_admissible([k8s, light_b], in_flight={}, limits=limits) == 0


def test_run_trials_batch_exclusive_not_starved_by_lights(tmp_path: Path) -> None:
    """A k8s trial queued before lights runs before those lights."""
    from nika.workflows.benchmark.run import _run_trials_batch

    order: list[str] = []
    lock = threading.Lock()

    def _fake_run(trial: Trial, **_kwargs: object) -> None:
        with lock:
            order.append(trial.row["scenario"])
        time.sleep(0.05)

    trials = [
        _trial(scenario="dc_clos", trial_index=1, case_index=0),
        _trial(scenario="k8s_lab", trial_index=1, case_index=1),
        *[
            _trial(scenario="dc_clos", trial_index=1, case_index=i, case_key=f"c{i}")
            for i in range(2, 6)
        ],
    ]
    with patch(
        "nika.workflows.benchmark.run._run_trial_with_timeout",
        side_effect=_fake_run,
    ):
        failures = _run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="byo.langgraph",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=2,
            serialize_heavy=True,
        )

    assert failures == []
    assert order.index("k8s_lab") == 1


def test_run_trials_batch_serializes_two_clab(tmp_path: Path) -> None:
    from nika.workflows.benchmark.run import _run_trials_batch

    active_clab = 0
    max_clab = 0
    lock = threading.Lock()

    def _fake_run(trial: Trial, **_kwargs: object) -> None:
        nonlocal active_clab, max_clab
        with lock:
            active_clab += 1
            max_clab = max(max_clab, active_clab)
        try:
            time.sleep(0.08)
        finally:
            with lock:
                active_clab -= 1

    trials = [
        _trial(scenario="min3clos", trial_index=1, case_index=0),
        _trial(scenario="min3clos", trial_index=2, case_index=1),
    ]
    with patch(
        "nika.workflows.benchmark.run._run_trial_with_timeout",
        side_effect=_fake_run,
    ):
        failures = _run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="byo.langgraph",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=2,
            serialize_heavy=True,
        )

    assert failures == []
    assert max_clab == 1


def test_run_trials_batch_clab_exclusive_no_peer_sessions(tmp_path: Path) -> None:
    """While a containerlab trial runs, light must not overlap."""
    from nika.workflows.benchmark.run import _run_trials_batch

    max_active = 0
    active = 0
    lock = threading.Lock()
    clab_alone = threading.Event()

    def _fake_run(trial: Trial, **_kwargs: object) -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if resource_class(trial) == CLASS_CLAB:
                assert active == 1, "clab overlapped another session"
                clab_alone.set()
        try:
            time.sleep(0.12)
        finally:
            with lock:
                active -= 1

    trials = [
        _trial(scenario="min3clos", trial_index=1, case_index=0),
        _trial(scenario="dc_clos", trial_index=1, case_index=1),
        _trial(scenario="dc_clos", trial_index=2, case_index=2),
    ]
    with patch(
        "nika.workflows.benchmark.run._run_trial_with_timeout",
        side_effect=_fake_run,
    ):
        failures = _run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="byo.langgraph",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=3,
            serialize_heavy=True,
        )

    assert failures == []
    assert clab_alone.is_set()
    assert max_active >= 1


def test_run_trials_batch_waits_for_clab_before_light(tmp_path: Path) -> None:
    """Light after two clab trials starts only after the active clab finishes."""
    from nika.workflows.benchmark.run import _run_trials_batch

    light_started_while_clab = threading.Event()
    first_clab_done = threading.Event()
    lock = threading.Lock()
    clab_active = 0

    def _fake_run(trial: Trial, **_kwargs: object) -> None:
        nonlocal clab_active
        cls = resource_class(trial)
        if cls == CLASS_CLAB:
            with lock:
                clab_active += 1
                assert clab_active == 1
            try:
                time.sleep(0.2)
            finally:
                with lock:
                    clab_active -= 1
                    first_clab_done.set()
        else:
            with lock:
                if clab_active > 0:
                    light_started_while_clab.set()
            time.sleep(0.05)

    trials = [
        _trial(scenario="min3clos", trial_index=1, case_index=0),
        _trial(scenario="min3clos", trial_index=2, case_index=1),
        _trial(scenario="dc_clos", trial_index=1, case_index=2),
    ]
    with patch(
        "nika.workflows.benchmark.run._run_trial_with_timeout",
        side_effect=_fake_run,
    ):
        failures = _run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="byo.langgraph",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=2,
            serialize_heavy=True,
        )

    assert failures == []
    assert first_clab_done.is_set()
    assert not light_started_while_clab.is_set()


def test_run_trials_batch_k8s_exclusive_no_peer_sessions(tmp_path: Path) -> None:
    """While a k8s trial runs, light must not overlap."""
    from nika.workflows.benchmark.run import _run_trials_batch

    max_active = 0
    active = 0
    lock = threading.Lock()
    k8s_alone = threading.Event()

    def _fake_run(trial: Trial, **_kwargs: object) -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if resource_class(trial) == CLASS_K8S and active == 1:
                k8s_alone.set()
            assert not (resource_class(trial) == CLASS_K8S and active > 1), (
                "k8s overlapped another session"
            )
        try:
            time.sleep(0.12)
        finally:
            with lock:
                active -= 1

    trials = [
        _trial(scenario="k8s_lab", trial_index=1, case_index=0),
        _trial(scenario="dc_clos", trial_index=1, case_index=1),
        _trial(scenario="dc_clos", trial_index=2, case_index=2),
    ]
    with patch(
        "nika.workflows.benchmark.run._run_trial_with_timeout",
        side_effect=_fake_run,
    ):
        failures = _run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="byo.langgraph",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=4,
            serialize_heavy=True,
        )

    assert failures == []
    assert k8s_alone.is_set()
    # Lights may overlap each other after k8s finishes, but never with k8s.
    assert max_active >= 1


def test_run_trials_batch_large_exclusive_no_peer_sessions(tmp_path: Path) -> None:
    """While a topo_size=l trial runs, other sessions must not overlap."""
    from nika.workflows.benchmark.run import _run_trials_batch

    active = 0
    lock = threading.Lock()
    large_alone = threading.Event()

    def _fake_run(trial: Trial, **_kwargs: object) -> None:
        nonlocal active
        with lock:
            active += 1
            if resource_class(trial) == CLASS_LARGE:
                assert active == 1, "large overlapped another session"
                large_alone.set()
        try:
            time.sleep(0.12)
        finally:
            with lock:
                active -= 1

    trials = [
        Trial(
            case_index=0,
            trial_index=1,
            row={
                "scenario": "dc_clos",
                "problem": "x",
                "topo_size": "l",
                "inject": {},
            },
            case_key="dc_clos__x__l",
            trial_id="dc_clos__x__l__t01",
        ),
        _trial(scenario="dc_clos", trial_index=1, case_index=1),
        _trial(scenario="dc_clos", trial_index=2, case_index=2),
    ]
    with patch(
        "nika.workflows.benchmark.run._run_trial_with_timeout",
        side_effect=_fake_run,
    ):
        failures = _run_trials_batch(
            trials,
            continue_on_error=True,
            case_timeout=0,
            agent_type="byo.langgraph",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(tmp_path),
            session_tag=None,
            release_meta=None,
            max_workers=4,
            serialize_heavy=True,
        )

    assert failures == []
    assert large_alone.is_set()
