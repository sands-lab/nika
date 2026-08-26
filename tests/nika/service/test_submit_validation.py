from __future__ import annotations

from nika.service.mcp_server.common.task_server import validate_root_cause_choices

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
