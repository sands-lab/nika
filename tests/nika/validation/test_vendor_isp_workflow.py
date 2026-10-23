"""Native vendors execute the shared ISP synthesis and verification workflow."""

from copy import deepcopy

import pytest

from tests.support.scenario_validation import assert_scenario_acceptance

from nika.workflows.validation.benchmark_routing import (
    build_benchmark_isp_experiment,
    isp_catalog_profiles,
)


NATIVE_ISP_CASES = [
    (vendor, topology, bgp_mode, "ospf")
    for vendor in ("iosxr", "routeros")
    for topology, bgp_mode in (
        ("arpanet196912", "none"),
        ("arpanet196912", "ibgp_rr"),
        ("dataxchange", "ebgp"),
    )
] + [("iosxr", "arpanet196912", "ibgp_rr", "isis")]


@pytest.mark.e2e
@pytest.mark.parametrize("vendor,topology,bgp_mode,igp", NATIVE_ISP_CASES)
def test_native_isp_synthesis_execution_and_recovery(
    vendor, topology, bgp_mode, igp, tmp_path, netcomplete_image, batfish_verifier
):
    import docker
    from docker.errors import ImageNotFound

    from nika.net_env.iosxr_simple_bgp.lab import IMAGE as xrd_image
    from nika.net_env.utils.routeros.common import IMAGE as routeros_image
    from nika.workflows.validation.scenario import validate_scenario_design

    client = docker.from_env()
    try:
        try:
            client.images.get(xrd_image if vendor == "iosxr" else routeros_image)
        except ImageNotFound:
            pytest.skip(f"Install the licensed {vendor} image first")
    finally:
        client.close()
    profile = deepcopy(
        next(
            p
            for p in isp_catalog_profiles().values()
            if p["scenario"] == f"isp_{topology}"
        )
    )
    profile["deployment"].update(igp=igp, bgp_mode=bgp_mode, device_profile=vendor)
    spec = build_benchmark_isp_experiment(profile)
    summary = validate_scenario_design(
        spec,
        tmp_path,
        runtime=True,
        image=netcomplete_image,
        batfish_verifier=batfish_verifier,
    )
    assert_scenario_acceptance(spec, summary, tmp_path, runtime=True)
    assert summary["synthesis"]["backend"] == "netcomplete"


@pytest.mark.contract
def test_benchmark_profiles_keep_vendor_identity_and_original_fault(tmp_path):
    import yaml

    from nika.workflows.validation.benchmark_routing import benchmark_isp_profiles

    manifest = tmp_path / "cases.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "cases": [
                    {
                        "scenario": "isp_arpanet196912",
                        "problem": "link_down",
                        "igp": "ospf",
                        "bgp_mode": "none",
                        "backend": "kathara",
                        "device_profile": vendor,
                        "inject": {"host_name": "sri", "intf_name": port},
                    }
                    for vendor, port in [
                        ("frr", "eth0"),
                        ("iosxr", "eth0"),
                        ("routeros", "eth1"),
                    ]
                ]
            }
        )
    )
    profiles = benchmark_isp_profiles([manifest])
    base = "isp_arpanet196912--ospf--none--kathara"
    assert set(profiles) == {base, base + "--iosxr", base + "--routeros"}
    for key, profile in profiles.items():
        spec = build_benchmark_isp_experiment(profile)
        (incident,) = spec.incidents
        original = profile["benchmark_cases"][0]["inject"]
        assert (incident.host_name, incident.intf_name) == (
            original["host_name"],
            original["intf_name"],
        )
        profile["benchmark_cases"] = [
            {"problem": "ospf_area_misconfiguration", "inject": {"host_name": "sri"}}
        ]
        if spec.deployment.device_profile != "frr":
            with pytest.raises(ValueError, match="native fault bindings"):
                build_benchmark_isp_experiment(profile)
            from nika.workflows.benchmark.inject_resolve import validate_benchmark_case

            with pytest.raises(ValueError, match="native fault bindings"):
                validate_benchmark_case(
                    profile["scenario"],
                    "ospf_area_misconfiguration",
                    {"host_name": original["host_name"]},
                    isp_options=profile["deployment"],
                )
