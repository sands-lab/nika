from __future__ import annotations

import pytest

from nika.net_env.contract import (
    AdjacencyExpectation,
    ValidationContract,
    ValidationIntent,
)
from nika.net_env.isp.kathara.verify import (
    _parse_frr_isis_neighbors,
    verify_isp_contract,
)
from nika.net_env.isp.verify_support import intent_command
from nika.net_env.verify_collection import CommandResult, VerifyCollection

pytestmark = pytest.mark.unit


def test_parses_frr_isis_neighbor_table_with_level_and_state() -> None:
    output = """Area NIKA:
  System Id Interface L State Holdtime SNPA
  r2 eth0 2 Up 27 0000.0000.0002
  r3 eth1 2 Down 0 0000.0000.0003
"""

    assert _parse_frr_isis_neighbors(output) == [
        {"system_id": "r2", "interface": "eth0", "level": "2", "state": "Up"},
        {
            "system_id": "r3",
            "interface": "eth1",
            "level": "2",
            "state": "Down",
        },
    ]


def test_valid_empty_neighbor_table_means_no_neighbors() -> None:
    assert (
        _parse_frr_isis_neighbors(
            "Area NIKA:\n  System Id Interface L State Holdtime SNPA\n"
        )
        == []
    )


def test_malformed_cli_output_is_an_error() -> None:
    with pytest.raises(ValueError, match="table header not found"):
        _parse_frr_isis_neighbors("% Unknown command: show isis neighbor")


def _intent() -> ValidationIntent:
    return ValidationIntent(
        id="adj.isis.r1.r2.eth0",
        description="Expected IS-IS adjacency.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="isis",
            isis_level=2,
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.0",
            remote_address="10.0.0.1",
            local_interface="eth0",
            remote_interface="eth0",
            local_router_id="1.1.1.1",
            remote_router_id="2.2.2.2",
        ),
    )


def _runtime_report(output: str):
    intent = _intent()
    command = intent_command(intent).command
    collection = VerifyCollection(
        {("r1", command): CommandResult(output, 0)},
        nodes=["r1", "r2"],
        duration_ms=1,
    )
    contract = ValidationContract(
        contract_id="test.isis",
        scenario="test",
        design_source={"source": "unit test"},
        intents=(intent,),
    )
    return verify_isp_contract(collection, contract=contract, plan=None).results[0]


@pytest.mark.parametrize("identity", ["r2", "0000.0202.0202"])
def test_runtime_intent_collects_and_accepts_exact_level2_up_neighbor(identity) -> None:
    output = f"""Area NIKA:
  System Id Interface L State Holdtime SNPA
  {identity} eth0 2 Up 27 0000.0000.0002
"""

    result = _runtime_report(output)

    assert result.status == "passed"
    assert result.evidence["command"] == "vtysh -c 'show isis neighbor'"


@pytest.mark.parametrize(
    "row",
    [
        "r2 eth1 2 Up 27 0000.0000.0002",  # parallel link on the wrong local port
        "r2 eth0 2 Down 0 0000.0000.0002",
        "r3 eth0 2 Up 27 0000.0000.0003",
    ],
)
def test_runtime_intent_rejects_parallel_down_and_wrong_peer_rows(row: str) -> None:
    output = f"Area NIKA:\n  System Id Interface L State Holdtime SNPA\n  {row}\n"

    assert _runtime_report(output).status == "failed"


def test_runtime_intent_reports_malformed_output_as_error() -> None:
    result = _runtime_report("% Unknown command: show isis neighbor")

    assert result.status == "error"
    assert "table header not found" in (result.reason or "")
