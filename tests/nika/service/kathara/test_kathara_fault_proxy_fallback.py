from __future__ import annotations

from nika.service.kathara import base_api


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
