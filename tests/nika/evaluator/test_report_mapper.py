"""Unstructured submission: answer parsing, majority vote, traces, and mode settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nika.evaluator.report_mapper import agree, parse_answer, vote
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
    with pytest.raises(ValidationError, match="apply only"):
        AgentSubmissionSettings(temperature=0.7)


def test_report_mapper_traces_samples_and_vote(tmp_path, monkeypatch) -> None:
    import asyncio
    import json

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from nika.evaluator.report_mapper import MapperModel, ReportMapper

    link = json.dumps(_LINK_DOWN)
    replies = ["no idea", link, '{"is_anomaly": false, "root_causes": []}', link]
    recorded = []

    async def record(answer, **_):
        recorded.append(answer)
        return "Submitted"

    monkeypatch.setattr("nika.evaluator.report_mapper._record", record)
    mapper = ReportMapper(
        models=[
            MapperModel(
                llm=FakeListChatModel(responses=replies),
                provider="custom",
                model="m",
                samples=3,
            )
        ]
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


def test_mapper_model_client_targets_custom_endpoint(monkeypatch) -> None:
    from nika.evaluator.model_client import load_model

    monkeypatch.setenv("NIKA_CUSTOM_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.delenv("NIKA_CUSTOM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = load_model("custom", "qwen", max_tokens=512)
    assert llm.model_name == "qwen"
    assert llm.openai_api_base == "http://vllm:8000/v1"
    # max_tokens stays max_tokens: not every OpenAI-compatible server accepts
    # max_completion_tokens.
    assert llm.extra_body == {"max_tokens": 512} and llm.max_tokens is None
    assert llm.streaming and llm.stream_usage


_HEALTHY = {"is_anomaly": False, "root_causes": []}


@pytest.mark.parametrize(
    ("answers", "weights", "min_agreement", "chosen"),
    [
        # Most models win, whatever the weights.
        ([_HEALTHY, _LINK_DOWN, _LINK_DOWN], [10, 1, 1], 1, _LINK_DOWN),
        # Ties go to the highest-weight supporter, then to configuration order.
        ([_HEALTHY, _LINK_DOWN], [1, 10], 1, _LINK_DOWN),
        ([_HEALTHY, _LINK_DOWN], [1, 1], 1, _HEALTHY),
        # A 2-2 split meets min_agreement 2; the weight breaks the tie.
        ([_HEALTHY, _HEALTHY, _LINK_DOWN, _LINK_DOWN], [1, 1, 5, 1], 2, _LINK_DOWN),
        # Disagreeing models, or an abstaining one, miss min_agreement 2.
        ([_HEALTHY, _LINK_DOWN], [1, 1], 2, None),
        ([_LINK_DOWN, None], [1, 1], 2, None),
        ([None, None], [1, 1], 1, None),
    ],
)
def test_agree_votes_across_models(answers, weights, min_agreement, chosen) -> None:
    assert agree(answers, weights, min_agreement=min_agreement)[0] == chosen


def test_submission_settings_mappers() -> None:
    single = AgentSubmissionSettings(
        mode="unstructured", provider="custom", model="m", samples=3
    )
    # A single mapper keeps the run identity it had before mappers existed.
    assert single.identity() == {
        "mode": "unstructured",
        "provider": "custom",
        "model": "m",
        "samples": 3,
    }
    assert [m.model for m in single.mapper_settings()] == ["m"]
    mappers = [
        {"provider": "anthropic", "model": "a", "weight": 10},
        {"provider": "custom", "model": "q", "samples": 3, "temperature": 0.7},
    ]
    several = AgentSubmissionSettings(
        mode="unstructured", mappers=mappers, min_agreement=2
    )
    assert several.identity()["min_agreement"] == 2
    assert [m.weight for m in several.mapper_settings()] == [10, 1]
    with pytest.raises(ValidationError, match="not both"):
        AgentSubmissionSettings(
            mode="unstructured", provider="custom", model="m", mappers=mappers
        )
    with pytest.raises(ValidationError, match="between 1 and the number"):
        AgentSubmissionSettings(mode="unstructured", mappers=mappers, min_agreement=3)
    with pytest.raises(ValidationError, match="only to mode: unstructured"):
        AgentSubmissionSettings(min_agreement=2)


def test_report_mapper_votes_across_models(tmp_path, monkeypatch) -> None:
    import asyncio
    import json

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from nika.evaluator.report_mapper import MapperModel, ReportMapper

    link, healthy = json.dumps(_LINK_DOWN), json.dumps(_HEALTHY)
    recorded = []

    async def record(answer, **_):
        recorded.append(answer)
        return "Submitted"

    def model(name: str, replies: list[str], weight: float = 1) -> MapperModel:
        llm = FakeListChatModel(responses=replies)
        return MapperModel(
            llm=llm, provider="custom", model=name, samples=len(replies), weight=weight
        )

    monkeypatch.setattr("nika.evaluator.report_mapper._record", record)
    mapper = ReportMapper(
        models=[
            model("a", [healthy]),
            # Its own majority is link_down; it votes once.
            model("b", [link, healthy, link], weight=5),
        ],
        min_agreement=1,
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
    votes = [r for r in rows if r["event"] == "submission_vote"]
    assert [(v["model"], v["chosen"]) for v in votes] == [
        ("a", _HEALTHY),
        ("b", _LINK_DOWN),
    ]
    (agreement,) = [r for r in rows if r["event"] == "submission_agreement"]
    # One vote each; b's weight breaks the tie.
    assert [(v["count"], v["models"]) for v in agreement["votes"]] == [
        (1, [2]),
        (1, [1]),
    ]
    assert agreement["chosen"] == _LINK_DOWN and recorded == [_LINK_DOWN]
