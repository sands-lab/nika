"""E2E: multi-fault injection through the production inject workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_multi_inject_params
from nika.utils.session_store import SessionStore
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

MULTI_CASES = [
    (
        "dc_clos",
        "s",
        ["mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"],
        None,
    ),
    ("dc_clos", "s", ["link_down", "host_missing_ip"], None),
    (
        "isp_abilene",
        "s",
        ["bgp_acl_block", "bgp_asn_misconfig"],
        {"igp": "ospf", "bgp_mode": "ebgp"},
    ),
    (
        "campus_lan",
        "s",
        ["dns_record_error", "host_incorrect_gateway"],
        None,
    ),
]


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestMultiFaultInjectWorkflow(IntegrationTestCase):
    @pytest.mark.parametrize(
        ("scenario", "topo_size", "problems", "isp_options"),
        MULTI_CASES,
    )
    def test_inject_verify_ground_truth(
        self,
        scenario: str,
        topo_size: str,
        problems: list[str],
        isp_options: dict[str, str] | None,
    ) -> None:
        params = resolve_multi_inject_params(
            problems,
            scenario,
            topo_size,
            seed=42,
            isp_options=isp_options,
        )
        env_args = ["-s", topo_size] if topo_size else []
        if isp_options:
            env_args.extend(
                [
                    "--igp",
                    isp_options["igp"],
                    "--bgp-mode",
                    isp_options["bgp_mode"],
                ]
            )
        session_id = None
        try:
            session_id = self._start_env(scenario, env_args)
            self._assert_session_ready(session_id, scenario)
            if scenario.startswith("isp_"):
                import time

                time.sleep(35)
            self._inject_multi_failure(problems, params, session_id=session_id)
            self._assert_multi_failure_injected(problems, session_id=session_id)
            row = SessionStore().get_session(session_id)
            gt = json.loads(
                (Path(row["session_dir"]) / "ground_truth.json").read_text()
            )
            assert len(gt["root_causes"]) == len(problems)
        finally:
            if session_id is not None:
                self._close_session(session_id)
