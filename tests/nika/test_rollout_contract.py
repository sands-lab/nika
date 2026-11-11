import pytest

from nika.rollout import RolloutRequest, RolloutResult
from nika.config import resolve_state_dir

pytestmark = pytest.mark.unit


def test_episode_contract_accepts_native_and_firecracker():
    for backend in ("native", "firecracker"):
        request = RolloutRequest.model_validate(
            {
                "request_id": "policy/task-42/attempt-2",
                "task": {"scenario": "dc_clos", "problem": "healthy", "topo_size": "s"},
                "run_config": {
                    "agent": {"type": "external"},
                    "nika": {"execution": {"mode": backend}},
                },
            }
        )
        assert RolloutRequest.model_validate_json(request.model_dump_json()) == request
    result = RolloutResult(
        request_id="attempt",
        request_sha256="a",
        outcome="infra_failed",
        reward=None,
        returncode=1,
        artifacts={},
    )
    assert result.reward is None


def test_worktree_state_override_requires_absolute_path(monkeypatch, tmp_path):
    monkeypatch.setenv("NIKA_STATE_DIR", str(tmp_path))
    assert resolve_state_dir() == tmp_path
    monkeypatch.setenv("NIKA_STATE_DIR", "relative")
    with pytest.raises(ValueError, match="absolute"):
        resolve_state_dir()


@pytest.mark.parametrize(
    ("outcome", "metrics", "reward"),
    [
        ("success", {"rca_f1": 0.25}, 0.25),
        ("agent_failed", {}, 0.0),
        ("infra_failed", {}, None),
    ],
)
def test_rollout_preserves_native_outcomes_and_refuses_output_reuse(
    tmp_path, monkeypatch, outcome, metrics, reward
):
    import json
    from types import SimpleNamespace
    from nika.rollout import api

    output = tmp_path / "attempt"
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        trial = output / "native/trials/trial"
        trial.mkdir(parents=True)
        (trial / "run.json").write_text(
            json.dumps({"outcome": outcome, "session_id": "owned"})
        )
        if metrics:
            (trial / "eval_metrics.json").write_text(json.dumps(metrics))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(api.subprocess, "run", execute)
    request = RolloutRequest(
        request_id="policy/task/attempt",
        task={"scenario": "dc_clos", "problem": "healthy"},
        run_config={},
    )
    result = api.run_rollout(request, output)
    assert result.outcome == outcome and result.reward == reward
    assert result.session_id == "owned"
    assert (output / result.artifacts["run.json"]).is_file()
    with pytest.raises(FileExistsError):
        api.run_rollout(request, output)
    assert len(commands) == 1
