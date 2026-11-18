"""Scenario-declared PingMesh expectations match each scenario's design."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nika.net_env.dc_clos.lab import DCClos
from nika.net_env.enterprise_branch.lab import EnterpriseBranch
from nika.net_env.p4_dc_gateway.lab import P4DcGateway
from nika.net_env.probe_intent import ProbeIntent

pytestmark = pytest.mark.unit


def _intent(module: str, factory) -> ProbeIntent:
    with patch(f"{module}.Kathara.get_instance", return_value=object()):
        intent = factory().probe_intent()
    assert intent is not None
    return intent


class ScenarioProbeIntentTest:
    def test_enterprise_isolates_local_only_vrfs_and_declares_wan_rtt(self) -> None:
        intent = _intent(
            "nika.net_env.enterprise_branch.lab", lambda: EnterpriseBranch("s")
        )
        isolated = intent.isolation_matcher()

        assert isolated("br1_guest_pc", "hq_corp_pc")
        assert isolated("br1_guest_pc", "br1_corp_pc")
        assert isolated("br1_guest_pc", "hq_guest_pc")
        assert not isolated("br1_corp_pc", "hq_srv")
        assert not isolated("br1_corp_pc", "br2_corp_pc")
        assert intent.max_expected_rtt_ms == 160

    def test_dc_clos_declares_no_isolation(self) -> None:
        # Distinct super-spine ASNs let every external client reach the others.
        with patch(
            "nika.net_env.dc_clos.lab.Kathara.get_instance", return_value=object()
        ):
            assert DCClos("m").probe_intent() is None

    def test_p4_gateway_is_north_south_only(self) -> None:
        intent = _intent("nika.net_env.p4_dc_gateway.lab", lambda: P4DcGateway("s"))
        isolated = intent.isolation_matcher()

        assert isolated("client_1", "client_2")
        assert isolated("service_1_1", "service_2_1")
        assert not isolated("client_1", "service_1_1")
        assert not isolated("service_1_1", "service_1_2")
