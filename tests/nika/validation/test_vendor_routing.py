"""One routing completion workflow binds native vendor configuration syntax."""

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.validation.batfish.vendor_bgp import project_routeros_bgp, vendor_configs
from nika.workflows.validation.direct_bgp import build_direct_bgp_experiment
from nika.workflows.validation.routing import validate_routing_design

PLATFORMS = [("iosxr_simple_bgp", "iosxr-bgp"), ("routeros_simple_bgp", "routeros-bgp")]


@pytest.mark.contract
@pytest.mark.parametrize("scenario,model", PLATFORMS)
def test_export_only_vendor_rejects_import_parameters(scenario, model):
    env = get_net_env_instance(scenario)
    node, router = next(iter(env.BGP_ROUTERS.items()))
    with pytest.raises(ValueError, match="BGP exports only"):
        get_net_env_instance(
            scenario,
            routing_parameters={
                # Exports are complete and valid: the unsupported import must
                # cause rejection rather than disappear during rendering.
                "bgp_exports": [
                    {
                        "node": name,
                        "neighbor": str(declaration["peer_ip"]),
                        "terms": [
                            {
                                "prefix": str(declaration["pc_ip"].network),
                                "permit": True,
                            }
                        ],
                    }
                    for name, declaration in env.BGP_ROUTERS.items()
                ],
                "bgp_imports": [
                    {
                        "node": node,
                        "neighbor": str(router["peer_ip"]),
                        "terms": [
                            {
                                "prefix": str(router["pc_ip"].network),
                                "permit": True,
                                "local_preference": 10,
                            }
                        ],
                    }
                ],
            },
        )


@pytest.mark.contract
@pytest.mark.parametrize("scenario,model", PLATFORMS)
def test_original_vendor_requirements_are_platform_independent(scenario, model):
    spec = build_direct_bgp_experiment(scenario, routing_model=model)
    assert spec.completion.origins == {
        "195.11.14.0/24": "router1",
        "200.1.1.0/24": "router2",
    }
    assert len(spec.healthy.intents) == 4
    assert {i.property for i in spec.incidents[0].requirements.intents} == {"isolation"}
    assert set(spec.counterexamples[0].expected_failed_ids) == {
        i.id for i in spec.healthy.intents
    }
    # Original unsolved startup configuration and failure endpoints are retained.
    env = get_net_env_instance(scenario)
    configs = vendor_configs(env)
    assert all("NIKA-OUT" not in config for config in configs.values())
    assert (
        env.BGP_INTERFACE_MAP[spec.incidents[0].intf_name] == env.BGP_PEERING_INTERFACE
    )


@pytest.mark.unit
def test_routeros_projection_rejects_unmodeled_forwarding():
    with pytest.raises(ValueError, match="Unsupported RouterOS statement"):
        project_routeros_bgp("router1", "/ip firewall filter add action=drop\n")


@pytest.mark.integration
@pytest.mark.parametrize("config_generator", [None, "netlab"])
@pytest.mark.parametrize("scenario,model", PLATFORMS)
def test_vendor_netcomplete_and_batfish(
    scenario, model, config_generator, tmp_path, netcomplete_image
):
    spec = build_direct_bgp_experiment(scenario, routing_model=model)
    summary = validate_routing_design(
        spec, tmp_path, image=netcomplete_image, config_generator=config_generator
    )
    assert summary["status"] == "passed"
    assert summary["validation_scope"] == "static-only"
    assert summary["synthesis"]["parameters"] == {"bgp_exports": 2}
    assert summary["static"] == {
        "healthy": {"passed": 4, "failed": 0},
        "transit-down": {"passed": 2, "failed": 0},
    }
    assert summary["counterexamples"]["wrong-peer-asn"]["status"] == "rejected"


@pytest.mark.e2e
@pytest.mark.parametrize("config_generator", [None, "netlab"])
@pytest.mark.parametrize("scenario,model", PLATFORMS)
def test_native_vendor_completion_execution_and_link_recovery(
    scenario, model, config_generator, tmp_path, netcomplete_image
):
    import docker
    from docker.errors import ImageNotFound

    env = get_net_env_instance(scenario)
    client = docker.from_env()
    try:
        for machine in env.lab.machines.values():
            try:
                client.images.get(machine.get_image())
            except ImageNotFound:
                pytest.skip(f"Native vendor image unavailable: {machine.get_image()}")
    finally:
        client.close()
    spec = build_direct_bgp_experiment(scenario, routing_model=model)
    summary = validate_routing_design(
        spec,
        tmp_path,
        runtime=True,
        image=netcomplete_image,
        config_generator=config_generator,
    )
    assert summary["runtime"]["healthy"]["passed"] == 4
    assert summary["runtime"]["transit-down"]["passed"] == 2
    assert summary["runtime"]["transit-down-recovered"]["passed"] == 4
