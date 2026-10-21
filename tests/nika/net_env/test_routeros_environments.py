"""RouterOS profiles keep each lab's addressing and protocol intents."""

import pytest

from nika.net_env.utils.routeros.common import CONFIG_FILE_PATH


def _routeros_routers(env):
    for name, identity in env.machine_identities.items():
        if "routeros" in identity.capabilities:
            yield name, env.lab.machines[name]


def _assert_vrnetlab_wiring(env):
    routers = dict(_routeros_routers(env))
    assert routers
    for name, machine in routers.items():
        # vrnetlab blocks boot until exactly CLAB_INTFS data interfaces exist.
        assert machine.meta["envs"]["CLAB_INTFS"] == str(len(machine.interfaces) - 1)
        assert machine.interfaces[0].link.name == f"{name}_reserved0"
        assert f"/system identity set name={name}" in machine.fs.readtext(
            CONFIG_FILE_PATH
        )
    return routers


@pytest.mark.unit
def test_dc_clos_routeros_renders_every_ebgp_session():
    from nika.net_env.dc_clos.lab import DCClos

    env = DCClos(topo_size="s", device_profile="routeros")
    routers = _assert_vrnetlab_wiring(env)
    assert set(routers) == set(env.bgp_neighbors)
    for name, peers in env.bgp_neighbors.items():
        config = routers[name].fs.readtext(CONFIG_FILE_PATH)
        for peer in peers:
            assert f"local.role=ebgp remote.address={peer} " in config
    leaf = routers["leaf_router_0_0"].fs.readtext(CONFIG_FILE_PATH)
    assert "list=BGP-NETWORKS address=10.0.0.0/24" in leaf
    assert "address=10.0.0.1/24 interface=ether4" in leaf
    with pytest.raises(ValueError, match="supports topo_size s only"):
        DCClos(topo_size="m", device_profile="routeros")


@pytest.mark.unit
def test_campus_routeros_keeps_areas_bridges_and_dhcp_relay():
    from nika.net_env.campus_lan.lab import CampusLan

    env = CampusLan(topo_size="s", device_profile="routeros")
    routers = _assert_vrnetlab_wiring(env)
    core = routers["router_core_1"].fs.readtext(CONFIG_FILE_PATH)
    assert "area-id=0.0.0.0" in core and "area-id=0.0.0.1" in core
    dist = routers["router_dist_1_1"].fs.readtext(CONFIG_FILE_PATH)
    assert "address=10.1.1.1/24 interface=br0" in dist
    assert "dhcp-server=10.200.0.8 local-address=10.1.1.1" in dist
    assert "area=area-0.0.0.1 networks=10.1.1.0/24" in dist
    with pytest.raises(ValueError, match="supports topo_size s only"):
        CampusLan(topo_size="m", device_profile="routeros")


@pytest.mark.contract
def test_start_accepts_only_registered_profiles_outside_isp():
    from nika.workflows.env.start import _resolve_isp_kwargs

    flags = dict(
        topo=None,
        igp=None,
        metric_strategy=None,
        constant_metric=None,
        bgp_mode=None,
        rpki=None,
        backend=None,
    )
    assert _resolve_isp_kwargs("dc_clos", device_profile=None, **flags) == {}
    assert _resolve_isp_kwargs("dc_clos", device_profile="frr", **flags) == {}
    assert _resolve_isp_kwargs("campus_lan", device_profile="routeros", **flags) == {
        "device_profile": "routeros"
    }
    with pytest.raises(ValueError, match="supports device profiles frr; got"):
        _resolve_isp_kwargs("enterprise_branch", device_profile="routeros", **flags)
    with pytest.raises(ValueError, match="does not accept"):
        _resolve_isp_kwargs(
            "dc_clos", device_profile="routeros", **{**flags, "igp": "ospf"}
        )
