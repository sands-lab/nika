"""Unit tests for LabVerifyTimeoutError and verify polling progress."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nika.net_env.verify import LabVerifyTimeoutError, verify_lab_with_retry
from nika.utils.logger import bind_session_dir


class _FakeLab:
    name = "fake-lab"
    VERIFY_MAX_WAIT_SEC = 0.05
    VERIFY_RETRY_DELAY_SEC = 0.01

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def startup_verify_lab(self) -> dict[str, Any]:
        return self._result

    def verify_lab(self) -> dict[str, Any]:
        return self._result

    def metadata(self) -> dict[str, Any]:
        return {}


def test_verify_lab_with_retry_raises_structured_timeout(tmp_path: Path) -> None:
    bind_session_dir(tmp_path)
    result = {
        "verified": False,
        "scenario_name": "fake",
        "checks": {"nodes_deployed": True, "bgp": False},
        "details": {"bgp_router": "r1"},
    }
    with pytest.raises(LabVerifyTimeoutError) as exc_info:
        verify_lab_with_retry(_FakeLab(result))  # type: ignore[arg-type]
    err = exc_info.value
    assert err.failed_checks == {"bgp": False}
    assert err.last_result["details"]["bgp_router"] == "r1"
    assert err.max_wait_sec == 0.05


def test_network_change_helper_writes_jsonl(tmp_path: Path) -> None:
    from nika.utils.network_change_log import log_network_change

    bind_session_dir(tmp_path)
    log_network_change(
        "link down on leaf:eth1",
        mechanism="link_operstate",
        host="leaf",
        intf="eth1",
        action="down",
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "nika.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["event"] == "network_change"
    assert rows[0]["data"]["mechanism"] == "link_operstate"
    assert "leaf:eth1" in rows[0]["message"]
