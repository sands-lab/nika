"""Unit tests for ISP device_profile pairing (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.isp.profiles import (
    normalize_device_profile,
    validate_backend_profile,
)
from nika.workflows.env.start import _resolve_isp_kwargs

pytestmark = pytest.mark.unit


def test_iosxr_profile_normalizes() -> None:
    assert normalize_device_profile("iosxr") == "iosxr"
    assert normalize_device_profile("IOSXR") == "iosxr"


def test_kathara_iosxr_pair_allowed() -> None:
    validate_backend_profile("kathara", "iosxr")


def test_containerlab_iosxr_pair_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported isp pairing"):
        validate_backend_profile("containerlab", "iosxr")


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_native_isp_profiles_require_explicit_ospf(profile) -> None:
    kwargs = _resolve_isp_kwargs(
        "isp_abilene",
        topo=None,
        igp=None,
        metric_strategy=None,
        constant_metric=None,
        bgp_mode=None,
        rpki=None,
        device_profile=profile,
        backend="kathara",
    )
    assert kwargs["device_profile"] == profile
    from nika.net_env.net_env_pool import get_net_env_instance

    with pytest.raises(ValueError, match="Native ISP supports OSPF"):
        get_net_env_instance("isp_abilene", **kwargs)

    kwargs["igp"] = "ospf"
    env = get_net_env_instance("isp_abilene", **kwargs)
    assert env.device_profile == profile
    assert env.plan.igp == "ospf"
    assert env.ROUTING_MODELS == {f"{profile}-isp"}
