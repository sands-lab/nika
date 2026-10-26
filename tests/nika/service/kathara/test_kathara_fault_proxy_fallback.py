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


def test_fallback_resolves_session_from_lab_name_when_meta_not_given(
    monkeypatch,
) -> None:
    # MCP tools that construct the API from a lab name alone (p4rt_exec, ...)
    # must still reach the static fallback while a fault proxy is active.
    from nika.utils import session_store

    seen: dict = {}
    fallback_lab = object()

    class _Store:
        def list_running_sessions(self):
            return [{"session_id": "s1", "lab_name": "p4__x"}]

        def get_session(self, session_id):
            return {"scenario_name": "p4_dc_gateway", "metadata": {}}

    monkeypatch.setattr(session_store, "SessionStore", _Store)
    monkeypatch.setattr(base_api.Kathara, "get_instance", lambda: _LiveKathara())

    def _static(session_meta, lab_name):
        seen["meta"] = session_meta
        return fallback_lab

    monkeypatch.setattr(base_api, "_static_lab_from_session", _static)

    assert base_api.KatharaBaseAPI("p4__x").lab is fallback_lab
    assert seen["meta"]["scenario_name"] == "p4_dc_gateway"


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
