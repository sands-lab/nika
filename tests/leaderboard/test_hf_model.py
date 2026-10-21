"""Which Hugging Face model and revision a submission records."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.workflows.leaderboard import hf_model
from nika.workflows.leaderboard.hf_model import HfModelError, resolve_hf_model

pytestmark = pytest.mark.unit

LATEST = "a" * 40
PINNED = "b" * 40


@pytest.fixture(autouse=True)
def fake_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    def model_info(repo: str, revision: str | None):
        if repo.lower() != "org/model":
            return None
        return SimpleNamespace(id="Org/Model", sha=PINNED if revision else LATEST)

    monkeypatch.setattr(hf_model, "_model_info", model_info)


def test_vendor_api_run_records_nothing() -> None:
    run = {"llm_provider": "anthropic", "model": "claude-haiku-5-5"}
    assert resolve_hf_model(run, {}) == {}


def test_custom_endpoint_model_id_pins_latest_revision() -> None:
    run = {"llm_provider": "custom", "model": "org/model"}
    assert resolve_hf_model(run, {}) == {
        "hf_repo": "Org/Model",
        "hf_revision": LATEST,
        "hf_revision_source": "latest",
    }


def test_override_repo_and_revision() -> None:
    run = {"llm_provider": "custom", "model": "served-alias"}
    assert resolve_hf_model(run, {}, hf_model="org/model", hf_revision="v1") == {
        "hf_repo": "Org/Model",
        "hf_revision": PINNED,
        "hf_revision_source": "specified",
    }


@pytest.mark.parametrize("model", ["gpt-oss:120b", "other/unknown"])
def test_unidentified_custom_model_asks_for_hf_model(model: str) -> None:
    run = {"llm_provider": "custom", "model": model}
    with pytest.raises(HfModelError, match="--hf-model"):
        resolve_hf_model(run, {})
    assert resolve_hf_model(run, {}, proprietary=True) == {}
