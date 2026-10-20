"""NIKA-owned submission: answer parsing, majority vote, and mode settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.submission import parse_answer, vote
from nika.run_config.schema import AgentSubmissionSettings

pytestmark = pytest.mark.unit

_CATALOG = {"catalog_ids": {"link/a--b", "node/pc1"}, "fault_types": {"link_down"}}
_LINK_DOWN = {
    "is_anomaly": True,
    "root_causes": [{"resource_id": "link/a--b", "fault_type": "link_down"}],
}


def test_parse_answer_accepts_fenced_json_and_dedupes() -> None:
    text = (
        'Answer:\n```json\n{"is_anomaly": true, "root_causes": ['
        '{"resource_id": "link/a--b", "fault_type": "link_down"},'
        '{"resource_id": "link/a--b", "fault_type": "link_down"}]}\n```'
    )
    assert parse_answer(text, **_CATALOG) == (_LINK_DOWN, "")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("no fault found", "no JSON object"),
        ('{"is_anomaly": "yes", "root_causes": []}', "boolean"),
        ('{"is_anomaly": true, "root_causes": []}', "non-empty"),
        ('{"is_anomaly": false, "root_causes": [{}]}', "non-empty"),
        (
            '{"is_anomaly": true, "root_causes": '
            '[{"resource_id": "node/pc9", "fault_type": "link_down"}]}',
            "not in the supplied canonical resource inventory",
        ),
    ],
)
def test_parse_answer_rejects_invalid(text: str, error: str) -> None:
    answer, message = parse_answer(text, **_CATALOG)
    assert answer is None and error in message


def test_parse_answer_accepts_healthy() -> None:
    healthy = {"is_anomaly": False, "root_causes": []}
    assert parse_answer('{"is_anomaly": false, "root_causes": []}', **_CATALOG) == (
        healthy,
        "",
    )


def test_vote_prefers_count_then_earliest() -> None:
    healthy = {"is_anomaly": False, "root_causes": []}
    assert vote([healthy, _LINK_DOWN, _LINK_DOWN])[0] == {
        "answer": _LINK_DOWN,
        "count": 2,
    }
    assert vote([healthy, _LINK_DOWN])[0]["answer"] == healthy


def test_submission_settings_require_mapper_model_only_when_unstructured() -> None:
    assert AgentSubmissionSettings().mode == "structured"
    with pytest.raises(ValidationError, match="only to mode: unstructured"):
        AgentSubmissionSettings(model="m")
    with pytest.raises(ValidationError, match="requires provider and model"):
        AgentSubmissionSettings(mode="unstructured", provider="custom")
    with pytest.raises(ValidationError, match="samples must be >= 1"):
        AgentSubmissionSettings(
            mode="unstructured", provider="custom", model="m", samples=0
        )


def test_report_mapper_traces_samples_and_vote(tmp_path, monkeypatch) -> None:
    import asyncio
    import json

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from agent.submission import ReportMapper

    link = json.dumps(_LINK_DOWN)
    replies = ["no idea", link, '{"is_anomaly": false, "root_causes": []}', link]
    recorded = []

    async def record(answer, **_):
        recorded.append(answer)
        return "Submitted"

    monkeypatch.setattr("agent.submission._record", record)
    mapper = ReportMapper(
        llm=FakeListChatModel(responses=replies),
        provider="custom",
        model="m",
        samples=3,
    )
    context = {
        "resources": [{"id": i} for i in _CATALOG["catalog_ids"]],
        "fault_ontology": [{"id": "link_down"}],
    }
    asyncio.run(
        mapper.submit(
            "report",
            context,
            session_id="s",
            scenario_name="x",
            trace_dir=str(tmp_path),
        )
    )
    rows = [
        json.loads(r) for r in (tmp_path / "messages.jsonl").read_text().splitlines()
    ]
    samples = [r for r in rows if r["event"] == "submission_sample"]
    assert [(r["sample"], r["attempt"], r["answer"] is None) for r in samples] == [
        (1, 1, True),
        (1, 2, False),
        (2, 1, False),
        (3, 1, False),
    ]
    assert samples[0]["text"] == "no idea" and "no JSON object" in samples[0]["error"]
    (vote,) = [r for r in rows if r["event"] == "submission_vote"]
    assert vote["chosen"] == _LINK_DOWN and recorded == [_LINK_DOWN]
    assert [v["count"] for v in vote["votes"]] == [2, 1]
    assert vote["answers"][1] == {"is_anomaly": False, "root_causes": []}


def test_parse_answer_takes_last_answer_object_after_reasoning() -> None:
    text = (
        'The inventory uses ids like {"id": "node/pc1"}. Final answer: '
        '{"is_anomaly": true, "root_causes": '
        '[{"resource_id": "link/a--b", "fault_type": "link_down"}]} done {x}'
    )
    assert parse_answer(text, **_CATALOG) == (_LINK_DOWN, "")


def test_unstructured_requires_nika_agent() -> None:
    from agent.submission import attach_report_mapper

    with pytest.raises(ValueError, match="subclasses agent.base.NikaAgent"):
        attach_report_mapper(object(), object())
