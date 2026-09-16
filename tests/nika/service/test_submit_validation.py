from __future__ import annotations

from nika.mcp.servers.common.task_server import (
    SubmitRootCause,
    mcp,
    validate_root_cause_choices,
)

_LINK_ID = "link/pc1:eth0--router1:eth0"


class SubmitValidationTest:
    def test_accepts_catalog_pair(self) -> None:
        parsed, errors = validate_root_cause_choices(
            [{"resource_id": _LINK_ID, "fault_type": "link_down"}],
            catalog_ids={_LINK_ID, "node/pc1"},
            fault_types={"link_down", "host_missing_ip"},
        )
        assert errors == []
        assert parsed == [{"resource_id": _LINK_ID, "fault_type": "link_down"}]

    def test_constructs_id_from_resource_fields(self) -> None:
        parsed, errors = validate_root_cause_choices(
            [
                {
                    "resource": {
                        "kind": "link",
                        "name": "pc1:eth0--router1:eth0",
                    },
                    "fault_type": "link_down",
                }
            ],
            catalog_ids={_LINK_ID, "node/pc1"},
            fault_types={"link_down"},
        )
        assert errors == []
        assert parsed == [{"resource_id": _LINK_ID, "fault_type": "link_down"}]

    def test_rejects_unknown_resource(self) -> None:
        _parsed, errors = validate_root_cause_choices(
            [{"resource_id": "link/ghost:eth0--pc1:eth0", "fault_type": "link_down"}],
            catalog_ids={_LINK_ID},
            fault_types={"link_down"},
        )
        assert errors
        assert "canonical resource inventory" in errors[0]

    def test_rejects_unknown_fault_type(self) -> None:
        _parsed, errors = validate_root_cause_choices(
            [{"resource_id": "node/pc1", "fault_type": "not_a_fault"}],
            catalog_ids={"node/pc1"},
            fault_types={"link_down"},
        )
        assert errors
        assert "fault ontology" in errors[0]

    def test_submit_tool_schema_requires_root_cause_fields(self) -> None:
        tool = mcp._tool_manager._tools["submit"]
        params = tool.parameters
        root = params["properties"]["root_causes"]
        item_ref = root["anyOf"][0]["items"]["$ref"]
        assert item_ref.endswith("/SubmitRootCause")
        item = params["$defs"]["SubmitRootCause"]
        assert set(item["required"]) == {"resource_id", "fault_type"}
        assert item["properties"]["resource_id"]["type"] == "string"
        assert item["properties"]["fault_type"]["type"] == "string"
        try:
            SubmitRootCause.model_validate({})
            raise AssertionError("empty object should be invalid")
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            assert "resource_id" in str(exc) or "fault_type" in str(exc)
