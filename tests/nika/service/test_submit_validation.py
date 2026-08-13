from __future__ import annotations

from nika.service.mcp_server.common.task_server import validate_root_cause_choices


class SubmitValidationTest:
    def test_accepts_catalog_pair(self) -> None:
        parsed, errors = validate_root_cause_choices(
            [{"resource_id": "interface/pc1/eth0", "fault_type": "link_down"}],
            catalog_ids={"interface/pc1/eth0", "node/pc1"},
            fault_types={"link_down", "host_crash"},
        )
        assert errors == []
        assert parsed == [
            {"resource_id": "interface/pc1/eth0", "fault_type": "link_down"}
        ]

    def test_constructs_id_from_resource_fields(self) -> None:
        parsed, errors = validate_root_cause_choices(
            [
                {
                    "resource": {
                        "kind": "interface",
                        "node": "pc1",
                        "name": "eth0",
                    },
                    "fault_type": "link_down",
                }
            ],
            catalog_ids={"interface/pc1/eth0", "node/pc1"},
            fault_types={"link_down"},
        )
        assert errors == []
        assert parsed == [
            {"resource_id": "interface/pc1/eth0", "fault_type": "link_down"}
        ]

    def test_rejects_unknown_resource(self) -> None:
        _parsed, errors = validate_root_cause_choices(
            [{"resource_id": "interface/ghost/eth0", "fault_type": "link_down"}],
            catalog_ids={"interface/pc1/eth0"},
            fault_types={"link_down"},
        )
        assert errors
        assert "list_resources" in errors[0]

    def test_rejects_unknown_fault_type(self) -> None:
        _parsed, errors = validate_root_cause_choices(
            [{"resource_id": "node/pc1", "fault_type": "not_a_fault"}],
            catalog_ids={"node/pc1"},
            fault_types={"link_down"},
        )
        assert errors
        assert "list_avail_problems" in errors[0]
