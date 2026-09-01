from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nika.net_env.contract import (
    EntitySelector,
    NetworkEntity,
    SelectorCatalog,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
    ValidationReport,
    ValidationResult,
)


def _contract() -> ValidationContract:
    source = NetworkEntity(kind="endpoint", name="client", address="10.0.0.2")
    destination = NetworkEntity(
        kind="service", name="dns", address="10.0.1.53", node="server"
    )
    return ValidationContract(
        contract_id="test.contract",
        scenario="test",
        design_source={"seed": 7},
        intents=(
            ValidationIntent(
                id="reach.client.dns",
                description="Client can reach DNS.",
                property="reachability",
                expected="reachable",
                source=source,
                destination=destination,
                traffic=TrafficSelector(protocol="udp", destination_port=53),
            ),
        ),
    )


def test_contract_json_round_trip_is_stable(tmp_path) -> None:
    contract = _contract()
    path = contract.write(tmp_path / "contract.json")
    assert ValidationContract.load(path) == contract
    assert contract.to_json() == ValidationContract.load(path).to_json()
    payload = json.loads(path.read_text())
    assert payload["intents"][0]["traffic"]["destination_port"] == 53


def test_selector_expansion_is_sorted_and_concrete() -> None:
    catalog = SelectorCatalog(
        entities=(
            NetworkEntity(kind="node", name="r2"),
            NetworkEntity(kind="node", name="r1"),
        ),
        groups={"routers": ("r2", "r1")},
    )
    expanded = catalog.expand(EntitySelector(kind="group", value="routers"))
    assert [entity.name for entity in expanded] == ["r1", "r2"]


def test_contract_rejects_invalid_property_shape_and_order() -> None:
    with pytest.raises(ValidationError, match="only valid for TCP or UDP"):
        TrafficSelector(protocol="icmp", destination_port=80)
    with pytest.raises(ValidationError, match="require destination_port"):
        TrafficSelector(protocol="tcp")
    with pytest.raises(ValidationError, match="IPv4 prefix"):
        NetworkEntity(kind="prefix", name="missing")

    first = _contract().intents[0]
    second = first.model_copy(update={"id": "a.reach"})
    with pytest.raises(ValidationError, match="sorted by stable ID"):
        ValidationContract(
            contract_id="bad",
            scenario="test",
            design_source={},
            intents=(first, second),
        )


def test_report_status_uses_required_intents_only() -> None:
    required = _contract().intents[0]
    optional = required.model_copy(update={"id": "z.optional", "level": "optional"})
    contract = ValidationContract(
        contract_id="test.report",
        scenario="test",
        design_source={},
        intents=(required, optional),
    )
    report = ValidationReport.from_results(
        contract,
        "unit",
        (
            ValidationResult(
                intent=required.id,
                verifier="unit",
                status="passed",
                duration_ms=1,
            ),
            ValidationResult(
                intent=optional.id,
                verifier="unit",
                status="failed",
                reason="optional probe unavailable",
                duration_ms=1,
            ),
        ),
    )
    assert report.status == "passed"
