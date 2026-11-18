import pytest

from nika.net_env.synthesis.vendor_bgp import (
    render_iosxr_bgp_exports,
    render_routeros_bgp_exports,
)

pytestmark = pytest.mark.unit


XR_CONFIG = """hostname router1
route-policy PASS
 pass
end-policy
router bgp 1
 neighbor 193.10.11.2
  remote-as 2
  address-family ipv4 unicast
   route-policy PASS in
   route-policy PASS out
  !
 !
!
end
"""

ROS_CONFIG = (
    "/routing bgp connection add name=to-peer instance=default "
    "local.role=ebgp remote.address=193.10.11.2 remote.as=2 "
    "output.redistribute=connected\n"
)

PARAMETERS = {
    "bgp_exports": [
        {
            "node": "router1",
            "neighbor": "193.10.11.2",
            "terms": [
                {"prefix": "195.11.14.0/24", "permit": True},
                {"prefix": "198.51.100.0/24", "permit": False},
            ],
        }
    ]
}


def test_iosxr_export_replaces_only_outbound_pass_and_ends_with_drop():
    rendered = render_iosxr_bgp_exports(XR_CONFIG, "router1", PARAMETERS)

    assert "route-policy PASS in" in rendered
    assert "route-policy PASS out" not in rendered
    assert "route-policy EXPORT-router1-193-10-11-2 out" in rendered
    assert "if destination in EXPORT-router1-193-10-11-2-P1 then\n  done" in rendered
    assert "if destination in EXPORT-router1-193-10-11-2-P2 then\n  drop" in rendered
    permit = rendered.index("if destination in EXPORT-router1-193-10-11-2-P1 then")
    drop = rendered.index(" drop\nend-policy", permit)
    done = rendered.index("  done", permit)
    assert permit < done < drop


def test_routeros_export_chain_matches_exact_prefix_and_preserves_connected():
    rendered = render_routeros_bgp_exports(ROS_CONFIG, "router1", PARAMETERS)

    assert "output.redistribute=connected" in rendered
    assert "output.filter-chain=EXPORT-router1-193-10-11-2" in rendered
    assert "dst == 195.11.14.0/24 && dst-len == 24) { accept; }" in rendered
    assert "dst == 198.51.100.0/24 && dst-len == 24) { reject; }" in rendered
    assert rendered.startswith("/routing filter rule add chain=")
    assert 'rule="reject;"\n/routing bgp connection add' in rendered


@pytest.mark.parametrize(
    ("renderer", "config"),
    [
        (render_iosxr_bgp_exports, XR_CONFIG),
        (render_routeros_bgp_exports, ROS_CONFIG),
    ],
)
def test_vendor_export_renderers_reject_missing_or_unknown_neighbors(renderer, config):
    with pytest.raises(ValueError, match="cover every neighbor"):
        renderer(config, "router1", {"bgp_exports": []})

    unknown = {
        "bgp_exports": [
            {
                **PARAMETERS["bgp_exports"][0],
                "neighbor": "192.0.2.1",
            }
        ]
    }
    with pytest.raises(ValueError, match="cover every neighbor"):
        renderer(config, "router1", unknown)


def test_vendor_export_renderers_reject_other_parameter_kinds():
    parameters = {
        **PARAMETERS,
        "ospf_costs": [{"node": "router1", "interface": "eth0", "cost": 10}],
    }
    with pytest.raises(ValueError, match="BGP exports only"):
        render_iosxr_bgp_exports(XR_CONFIG, "router1", parameters)
    with pytest.raises(ValueError, match="BGP exports only"):
        render_routeros_bgp_exports(ROS_CONFIG, "router1", parameters)


def test_vendor_export_renderers_preserve_unknown_original_policy():
    xr = XR_CONFIG.replace("route-policy PASS out", "route-policy CUSTOM out")
    ros = ROS_CONFIG.replace(
        "output.redistribute=connected",
        "output.filter-chain=CUSTOM output.redistribute=connected",
    )
    with pytest.raises(ValueError, match="unknown IOS-XR export policy"):
        render_iosxr_bgp_exports(xr, "router1", PARAMETERS)
    with pytest.raises(ValueError, match="unknown RouterOS export filter"):
        render_routeros_bgp_exports(ros, "router1", PARAMETERS)
