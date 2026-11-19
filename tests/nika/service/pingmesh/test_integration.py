"""Continuous probes distinguish flow-selective drops from ICMP reachability."""

from __future__ import annotations

import time

import pytest

from nika.service.kathara.prometheus_api import prometheus_query_for
from nika.service.lab.host_api import create_host_api
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not docker_available(), reason="Docker is not available")
class TestContinuousMesh(IntegrationTestCase):
    def test_default_mesh_records_healthy_and_flow_selective_failures(self) -> None:
        session_id = self._start_env("dc_clos", ["-s", "s"])
        try:
            meta = self._assert_session_ready(session_id, "dc_clos")
            assert "pingmesh" in meta["scenario_params"]["telemetry"]
            api = create_host_api(lab_name=meta["lab_name"], session_meta=meta)

            def query(expression):
                return prometheus_query_for(api, query=expression, limit=100)

            deadline = time.monotonic() + 120
            healthy = []
            while time.monotonic() < deadline:
                healthy = query(
                    'min_over_time(probe_success{expected="reachable"}[30s])'
                )
                if healthy:
                    break
                time.sleep(5)
            assert healthy and all(row["value"] == 1 for row in healthy)

            self._inject_failure(
                "device_flow_blackhole",
                {"forwarding_device": "leaf_1", "intf_name": "eth0"},
                session_id=session_id,
            )
            deadline = time.monotonic() + 240
            tcp = []
            while time.monotonic() < deadline:
                tcp = query(
                    'min_over_time(probe_success{target="web_1",module="tcp_connect"}[2m]) == 0'
                )
                if tcp:
                    break
                time.sleep(10)
            assert tcp, "The recorded TCP probes did not observe the blackhole."
            icmp = query(
                'min_over_time(probe_success{target="web_1",module="icmp"}[1m])'
            )
            assert icmp and all(row["value"] == 1 for row in icmp)
        finally:
            self._close_session(session_id)
