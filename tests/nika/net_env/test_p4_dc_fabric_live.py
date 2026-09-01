"""Live Docker tests for p4_dc_fabric deploy, verify, and redeploy."""

from __future__ import annotations

from pathlib import Path

import pytest

from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.net_env.verify import http_ok, ping_ok
from nika.runtime.factory import runtime_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.scenario_failure_compat import write_probe_report

REPORT_PATH = Path("results/test/p4_dc_fabric_acceptance.json")


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4DcFabricLiveTest(IntegrationTestCase):
    def test_deploy_verify_redeploy_s(self) -> None:
        row = {
            "size": "s",
            "deployment_success": False,
            "p4runtime_session_success": False,
            "forwarding_verification": False,
            "ecmp_verification": False,
            "http_verification": False,
            "cleanup_redeploy": False,
        }
        session_id = self._start_env("p4_dc_fabric", ["-s", "s"])
        try:
            meta = self._assert_session_ready(session_id, "p4_dc_fabric")
            runtime = runtime_for_session(meta)
            model = build_clos_fabric_model("s")
            row["deployment_success"] = True
            from nika.net_env.p4_dc_fabric.verify import (
                verify_p4_dc_fabric_lab,
            )

            result = verify_p4_dc_fabric_lab(
                runtime, scenario_name="p4_dc_fabric", model=model
            )
            checks = result.get("checks") or {}
            row["p4runtime_session_success"] = bool(checks.get("p4runtime_consistent"))
            row["forwarding_verification"] = bool(
                checks.get("same_rack_ping") and checks.get("cross_rack_ping")
            )
            row["ecmp_verification"] = bool(checks.get("ecmp_multi_path"))
            row["http_verification"] = bool(checks.get("cross_rack_http"))
            assert result.get("verified"), checks
        finally:
            self._close_session(session_id)

        session_id = self._start_env("p4_dc_fabric", ["-s", "s"])
        try:
            self._assert_session_ready(session_id, "p4_dc_fabric")
            row["cleanup_redeploy"] = True
        finally:
            self._close_session(session_id)
        write_probe_report(REPORT_PATH, {"rows": [row]})
        assert all(row[k] for k in row if k != "size")


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4DcFabricScaleLiveTest(IntegrationTestCase):
    @pytest.mark.parametrize("topo_size", ["m", "l"])
    def test_deploy_and_verify_scale(self, topo_size: str) -> None:
        session_id = self._start_env("p4_dc_fabric", ["-s", topo_size])
        try:
            meta = self._assert_session_ready(session_id, "p4_dc_fabric")
            runtime = runtime_for_session(meta)
            model = build_clos_fabric_model(topo_size)  # type: ignore[arg-type]
            src = model.client_endpoints()[0]
            dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
            assert ping_ok(runtime, src.name, dst.ip)
            assert http_ok(runtime, src.name, f"http://{dst.ip}/")
        finally:
            self._close_session(session_id)
