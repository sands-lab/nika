"""Default probe paths for remaining telemetry coverage families."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import get_probe_path

pytestmark = pytest.mark.contract

_CASES = (
    ("isp_france", "s"),
    ("isp_pioro40", "s"),
    ("isp_dfn-bwin_ebgp_rtbh", "s"),
    ("isp_geant_ebgp_rpki", "s"),
    ("isp_ai3", "s"),
    ("isp_polska", "s"),
    ("dc_clos", "m"),
    ("sdn_l3_clos", "m"),
    ("p4_dc_fabric", "m"),
    ("p4_dc_gateway", "m"),
    ("campus_lan", "s"),
    ("enterprise_branch", "s"),
)


@pytest.mark.parametrize(("scenario", "topo_size"), _CASES)
def test_remaining_families_declare_probe_paths(scenario: str, topo_size: str) -> None:
    path = get_probe_path(scenario, topo_size=topo_size)
    assert path is not None
    assert path.src_host
    assert path.icmp_ip or path.dst_ip
