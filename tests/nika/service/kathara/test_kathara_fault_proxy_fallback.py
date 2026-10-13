from __future__ import annotations

import pytest

from nika.service.kathara import base_api

pytestmark = pytest.mark.unit


class _LiveKathara:
    def get_lab_from_api(self, *, lab_name: str):
        raise KeyError("nika-fp-transient-network")


def test_uses_static_lab_definition_when_fault_proxy_breaks_live_parser(
    monkeypatch,
) -> None:
    fallback_lab = object()
    monkeypatch.setattr(base_api.Kathara, "get_instance", lambda: _LiveKathara())
    monkeypatch.setattr(
        base_api,
        "_static_lab_from_session",
        lambda session_meta, lab_name: fallback_lab,
    )

    api = base_api.KatharaBaseAPI(
        "simple_bgp__test",
        session_meta={"scenario_name": "simple_bgp", "metadata": {}},
    )

    assert api.lab is fallback_lab


def test_static_lab_rebuilds_isp_from_persisted_session_params() -> None:
    # ISP sessions persist topo_size/topo, which the ISP lab constructor rejects.
    session_meta = {
        "scenario_name": "isp_dfn-bwin",
        "scenario_params": {
            "lab_name": "isp_dfn-bwin__test",
            "backend": "kathara",
            "topo_size": "s",
            "topo": "dfn-bwin",
            "igp": "isis",
            "bgp_mode": "none",
            "rpki": False,
            "device_profile": "frr",
        },
    }

    lab = base_api._static_lab_from_session(session_meta, "isp_dfn-bwin__test")

    assert lab.name == "isp_dfn-bwin__test"
    assert "berlin" in lab.machines
