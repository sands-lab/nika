"""CLI/workflow argument validation for isp (no Docker)."""

from __future__ import annotations

import pytest

from nika.workflows.env.start import _resolve_isp_kwargs


def _kwargs(**overrides):
    base = {
        "topo": None,
        "igp": None,
        "metric_strategy": None,
        "constant_metric": None,
        "bgp_mode": None,
        "rpki": None,
        "device_profile": None,
        "backend": None,
    }
    base.update(overrides)
    return base


def test_isp_defaults_applied() -> None:
    kwargs = _resolve_isp_kwargs("isp_abilene", **_kwargs())
    assert kwargs == {
        "igp": "isis",
        "metric_strategy": "constant",
        "constant_metric": 10,
        "bgp_mode": "none",
        "rpki": False,
        "device_profile": "frr",
    }


def test_isp_containerlab_defaults_nokia() -> None:
    kwargs = _resolve_isp_kwargs("isp_abilene", **_kwargs(backend="containerlab"))
    assert kwargs["device_profile"] == "nokia_srlinux"


def test_isp_rejects_kathara_nokia() -> None:
    with pytest.raises(ValueError, match="Unsupported isp pairing"):
        _resolve_isp_kwargs(
            "isp_abilene",
            **_kwargs(backend="kathara", device_profile="nokia_srlinux"),
        )


def test_isp_rejects_containerlab_frr() -> None:
    with pytest.raises(ValueError, match="Unsupported isp pairing"):
        _resolve_isp_kwargs(
            "isp_abilene", **_kwargs(backend="containerlab", device_profile="frr")
        )


def test_other_scenario_rejects_isp_flags() -> None:
    with pytest.raises(ValueError, match="does not accept"):
        _resolve_isp_kwargs("simple_bgp", **_kwargs(igp="ospf"))


def test_other_scenario_rejects_bgp_mode() -> None:
    with pytest.raises(ValueError, match="does not accept"):
        _resolve_isp_kwargs("simple_bgp", **_kwargs(bgp_mode="ibgp_rr"))


def test_other_scenario_ok_without_flags() -> None:
    assert _resolve_isp_kwargs("simple_bgp", **_kwargs()) == {}


def test_invalid_igp_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported IGP"):
        _resolve_isp_kwargs("isp_polska", **_kwargs(igp="bgp"))


def test_invalid_bgp_mode_rejected() -> None:
    with pytest.raises(ValueError, match="bgp_mode"):
        _resolve_isp_kwargs("isp_polska", **_kwargs(bgp_mode="confederation"))


def test_bgp_mode_ibgp_rr_accepted() -> None:
    kwargs = _resolve_isp_kwargs("isp_abilene", **_kwargs(bgp_mode="ibgp_rr"))
    assert kwargs["bgp_mode"] == "ibgp_rr"
    assert "topo" not in kwargs


def test_isp_named_rpki_rejects_overrides() -> None:
    with pytest.raises(ValueError, match="fixed protocol profile"):
        _resolve_isp_kwargs(
            "isp_abilene_ebgp_rpki", **_kwargs(bgp_mode="ebgp", rpki=True)
        )


def test_isp_base_rejects_rpki_flag() -> None:
    with pytest.raises(ValueError, match="named scenario"):
        _resolve_isp_kwargs("isp_abilene", **_kwargs(bgp_mode="ebgp", rpki=True))


def test_isp_rejects_explicit_topo() -> None:
    with pytest.raises(ValueError, match="omit --topo"):
        _resolve_isp_kwargs("isp_abilene", **_kwargs(topo="abilene"))


def test_isp_rejects_unknown_bgp_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported bgp_mode"):
        _resolve_isp_kwargs("isp_abilene", **_kwargs(bgp_mode="ebgp_rpki"))
