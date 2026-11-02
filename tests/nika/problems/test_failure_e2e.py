"""Parametrized failure inject → verify → symptom → recover E2E tests."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

import pytest

from tests.support.ci_depth import artifact_verify_only
from tests.support.failure_e2e import FailureE2ECase, run_failure_e2e
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    docker_image_available,
    linux_vrf_available,
    privileged_lab_supported,
)

pytestmark = [pytest.mark.e2e, pytest.mark.nightly]


@dataclass(frozen=True)
class _FlapScenarioCase:
    id: str
    scenario: str
    env_args: tuple[str, ...]
    topo_size: str = "s"
    isp_options: dict[str, str] | None = None
    require_clab: bool = False
    require_privileged: bool = False


FLAP_SCENARIOS = (
    _FlapScenarioCase("simple_bgp", "simple_bgp", (), ""),
    _FlapScenarioCase("dc_clos", "dc_clos", ("-s", "s")),
    _FlapScenarioCase("campus_lan", "campus_lan", ("-s", "s")),
    _FlapScenarioCase("enterprise_branch", "enterprise_branch", ("-s", "s")),
    _FlapScenarioCase("k8s_lab", "k8s_lab", (), "", require_privileged=True),
    _FlapScenarioCase("min3clos", "min3clos", (), "", require_clab=True),
    _FlapScenarioCase("p4_dc_fabric", "p4_dc_fabric", ("-s", "s")),
    _FlapScenarioCase("p4_dc_gateway", "p4_dc_gateway", ("-s", "s")),
    _FlapScenarioCase("sdn_l3_clos", "sdn_l3_clos", ("-s", "s")),
    _FlapScenarioCase(
        "isp-abilene-ebgp",
        "isp_abilene",
        ("--igp", "ospf", "--bgp-mode", "ebgp"),
        "s",
        isp_options={
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": False,
        },
    ),
    _FlapScenarioCase(
        "isp-abilene-ebgp-rpki",
        "isp_abilene_ebgp_rpki",
        (),
        "s",
        isp_options=None,
    ),
)
FLAP_BY_SCENARIO = {flap.scenario: flap for flap in FLAP_SCENARIOS}


def _flap_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="link_flap",
            scenario=flap.scenario,
            env_run_args=flap.env_args,
            topo_size=flap.topo_size or "s",
            inject_seed=0 if flap.scenario == "k8s_lab" else 1,
            isp_options=flap.isp_options,
        )
        for flap in FLAP_SCENARIOS
    ]


def _link_detach_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="link_detach",
            scenario=scenario,
            env_run_args=("-s", "s"),
            inject_seed=0,
        )
        for scenario in ("dc_clos", "enterprise_branch", "sdn_l3_clos")
    ]


def _incast_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="incast_traffic_network_limitation",
            scenario=scenario,
            env_run_args=("-s", "s"),
            inject_seed=1,
        )
        for scenario in ("p4_dc_gateway", "p4_dc_fabric")
    ]


CORE_E2E_CASES = (
    *(
        FailureE2ECase(
            problem="ospf_hello_interval_mismatch",
            scenario=scenario,
            env_run_args=(
                (("-s", "s") if scenario == "campus_lan" else ("--igp", "ospf"))
                + (("--device-profile", profile) if profile != "frr" else ())
            ),
            isp_options=(
                {
                    "igp": "ospf",
                    "bgp_mode": "none",
                    "rpki": False,
                    "device_profile": profile,
                }
                if scenario == "isp_abilene"
                else None
            ),
            inject_params=(
                {"host_name": "router_core_1", "intf_name": "eth2"}
                if scenario == "campus_lan"
                else None
            ),
        )
        for scenario in ("campus_lan", "isp_abilene")
        for profile in ("frr", "iosxr", "routeros")
    ),
    FailureE2ECase(
        problem="bgp_max_prefix_exceeded",
        scenario="isp_abilene",
        env_run_args=("--igp", "ospf", "--bgp-mode", "ebgp"),
        isp_options={"igp": "ospf", "bgp_mode": "ebgp", "rpki": False},
    ),
    FailureE2ECase(
        problem="ospf_neighbor_missing",
        scenario="campus_lan",
        env_run_args=("-s", "s"),
        inject_params={"host_name": "router_core_1"},
    ),
    FailureE2ECase(
        problem="ospf_neighbor_missing",
        scenario="isp_abilene",
        env_run_args=("--igp", "ospf"),
        isp_options={"igp": "ospf", "bgp_mode": "none", "rpki": False},
    ),
    *(
        FailureE2ECase(
            problem="bgp_max_prefix_exceeded",
            scenario=scenario,
            env_run_args=(
                "--igp",
                "ospf",
                "--bgp-mode",
                "ebgp",
                "--device-profile",
                profile,
            ),
            isp_options={
                "igp": "ospf",
                "bgp_mode": "ebgp",
                "rpki": False,
                "device_profile": profile,
            },
        )
        for scenario in ("isp_abilene", "isp_geant")
        for profile in ("iosxr", "routeros")
    ),
    *(
        FailureE2ECase(
            problem="ospf_neighbor_missing",
            scenario="campus_lan",
            env_run_args=("-s", "s", "--device-profile", profile),
            inject_params={"host_name": "router_core_1"},
        )
        for profile in ("iosxr", "routeros")
    ),
    *(
        FailureE2ECase(
            problem="ospf_neighbor_missing",
            scenario=scenario,
            env_run_args=("--igp", "ospf", "--device-profile", profile),
            isp_options={
                "igp": "ospf",
                "bgp_mode": "none",
                "rpki": False,
                "device_profile": profile,
            },
        )
        for scenario in (
            "isp_abilene",
            "isp_atlanta",
            "isp_dfn-bwin",
            "isp_dfn-gwin",
            "isp_di-yuan",
            "isp_nobel-us",
            "isp_pdh",
            "isp_polska",
        )
        for profile in ("iosxr", "routeros")
    ),
    FailureE2ECase(
        problem="receiver_resource_contention",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        param_overrides={"duration": "1"},
        sleep_after_inject_sec=3.0,
    ),
    FailureE2ECase(
        problem="tcp_syn_flood_attack",
        scenario="p4_dc_gateway",
        env_run_args=("-s", "s"),
        param_overrides={"duration": "1"},
        sleep_after_inject_sec=3.0,
    ),
    FailureE2ECase(
        problem="link_down",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="link_capacity_bottleneck",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="load_balancer_overload",
        scenario="campus_lan",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="sender_resource_contention",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="tcp_receive_window_limited",
        scenario="enterprise_branch",
        env_run_args=("-s", "m"),
        topo_size="m",
        inject_seed=1,
        param_overrides={"baseline_trials": "1", "iperf_duration_sec": "3"},
    ),
    FailureE2ECase(
        problem="device_flow_blackhole",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="device_flow_blackhole",
        scenario="dc_clos",
        env_run_args=("-s", "s", "--device-profile", "routeros"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="bgp_ttl_security_mismatch",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    *[
        FailureE2ECase(
            problem="ospf_area_range_suppression",
            scenario="campus_lan",
            topo_size=size,
            env_run_args=(
                "-s",
                size,
                *(("--device-profile", profile) if profile else ()),
            ),
            inject_params={"host_name": host, "area": "0.0.0.1"},
            inject_seed=1 if host == "router_core_1" else 2,
        )
        for profile, size, host in (
            (None, "s", "router_core_1"),
            (None, "s", "router_core_2"),
            ("iosxr", "s", "router_core_1"),
            ("routeros", "s", "router_core_2"),
        )
    ],
    *(
        FailureE2ECase(
            problem="bgp_md5_password_mismatch",
            scenario=scenario,
            env_run_args=("-s", "s", *profile_args),
            inject_params={"host_name": host},
        )
        for scenario, host in (
            ("dc_clos", "leaf_router_0_0"),
            ("enterprise_branch", "br1_edge"),
        )
        for profile_args in (
            (),
            ("--device-profile", "iosxr"),
            ("--device-profile", "routeros"),
        )
    ),
    *(
        FailureE2ECase(
            problem="bgp_md5_password_mismatch",
            scenario="isp_abilene",
            env_run_args=(
                "--igp",
                "isis" if mode == "ibgp_rr" else "ospf",
                "--bgp-mode",
                mode,
                *profile_args,
            ),
            isp_options={
                "igp": "isis" if mode == "ibgp_rr" else "ospf",
                "bgp_mode": mode,
                "rpki": False,
            },
            inject_params=(
                {"host_name": "atlang", "neighbor_ip": "10.0.0.5"}
                if mode == "ebgp"
                else {"host_name": "sttlng"}
            ),
            inject_seed=0 if mode == "ebgp" else 1,
        )
        for mode in ("ebgp", "ibgp_rr")
        for profile_args in (
            (),
            ("--device-profile", "iosxr"),
            ("--device-profile", "routeros"),
        )
    ),
    *(
        FailureE2ECase(
            problem="bgp_md5_password_mismatch",
            scenario=scenario,
            topo_size="",
            inject_params={"host_name": "router1"},
        )
        for scenario in ("iosxr_simple_bgp", "routeros_simple_bgp")
    ),
    # The adjacent-neighbor branch (campus_lan area 1) and the LSA-shadowing
    # branch (single-area ISP).
    FailureE2ECase(
        problem="ospf_duplicate_router_id",
        scenario="campus_lan",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="ospf_duplicate_router_id",
        scenario="isp_abilene",
        env_run_args=("--igp", "ospf", "--bgp-mode", "ebgp"),
        isp_options={"igp": "ospf", "bgp_mode": "ebgp", "rpki": False},
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="ospf_network_type_mismatch",
        scenario="campus_lan",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="ospf_network_type_mismatch",
        scenario="isp_abilene",
        env_run_args=("--igp", "ospf"),
        isp_options={"igp": "ospf", "bgp_mode": "none", "rpki": False},
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="ospf_network_type_mismatch",
        scenario="isp_abilene",
        env_run_args=("--igp", "ospf", "--device-profile", "routeros"),
        isp_options={
            "igp": "ospf",
            "bgp_mode": "none",
            "rpki": False,
            "device_profile": "routeros",
        },
        inject_params={"host_name": "atlang", "intf_name": "eth1"},
    ),
    FailureE2ECase(
        problem="ospf_network_type_mismatch",
        scenario="campus_lan",
        env_run_args=("-s", "s", "--device-profile", "routeros"),
        inject_params={"host_name": "router_core_1", "intf_name": "eth2"},
    ),
    FailureE2ECase(
        problem="switch_datapath_id_conflict",
        scenario="sdn_l3_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_service_target_port_mismatch",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_service_target_port_mismatch",
        scenario="llmd_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_service_selector_mismatch",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_service_selector_mismatch",
        scenario="llmd_lab",
        topo_size="",
        inject_seed=1,
    ),
    *(
        FailureE2ECase(
            problem="k8s_vxlan_neighbor_stale",
            scenario=scenario,
            topo_size="",
            inject_seed=1,
        )
        for scenario in ("k8s_lab", "llmd_lab")
    ),
    FailureE2ECase(
        problem="k8s_ingress_rate_limit_misconfig",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_ingress_class_mismatch",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    *(
        FailureE2ECase(
            problem="k8s_coredns_forward_loop",
            scenario=scenario,
            topo_size="",
        )
        for scenario in ("k8s_lab", "llmd_lab")
    ),
    *(
        FailureE2ECase(
            problem="k8s_readiness_probe_misconfig",
            scenario=scenario,
            topo_size="",
            inject_seed=1,
        )
        for scenario in ("k8s_lab", "llmd_lab")
    ),
    FailureE2ECase(
        problem="k8s_deployment_replicas_zero",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_deployment_replicas_zero",
        scenario="llmd_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="k8s_node_noexecute_taint",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="lb_advertisement_missing",
        scenario="k8s_lab",
        topo_size="",
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="ospf_duplicate_router_id",
        scenario="campus_lan",
        env_run_args=("-s", "s", "--device-profile", "routeros"),
        inject_params={"host_name": "router_dist_1_1"},
    ),
    FailureE2ECase(
        problem="ospf_duplicate_router_id",
        scenario="isp_abilene",
        env_run_args=(
            "--igp",
            "ospf",
            "--bgp-mode",
            "ebgp",
            "--device-profile",
            "iosxr",
        ),
        isp_options={"igp": "ospf", "bgp_mode": "ebgp", "rpki": False},
        inject_params={"host_name": "chinng"},
    ),
    FailureE2ECase(
        problem="bgp_ttl_security_mismatch",
        scenario="iosxr_simple_bgp",
        topo_size="",
        inject_params={"host_name": "router1"},
        sleep_after_inject_sec=30.0,
    ),
    FailureE2ECase(
        problem="bgp_ttl_security_mismatch",
        scenario="dc_clos",
        env_run_args=("-s", "s", "--device-profile", "iosxr"),
        inject_params={"host_name": "leaf_router_0_0"},
        sleep_after_inject_sec=30.0,
    ),
    FailureE2ECase(
        problem="lb_advertisement_missing",
        scenario="llmd_lab",
        topo_size="",
        inject_seed=1,
    ),
    # Native routers only: IOS XR's RFC 8212 default and RouterOS's
    # undefined filter chain.
    *(
        FailureE2ECase(
            problem="bgp_session_policy_missing",
            scenario="dc_clos",
            env_run_args=("-s", "s", "--device-profile", profile),
            inject_params={"host_name": "leaf_router_0_1"},
        )
        for profile in ("iosxr", "routeros")
    ),
    FailureE2ECase(
        problem="bgp_session_policy_missing",
        scenario="enterprise_branch",
        env_run_args=("-s", "s", "--device-profile", "routeros"),
        inject_params={"host_name": "br1_edge"},
    ),
    FailureE2ECase(
        problem="isis_metric_style_mismatch",
        scenario="isp_abilene",
        env_run_args=("--igp", "isis"),
        isp_options={"igp": "isis", "bgp_mode": "none", "rpki": False},
        inject_seed=1,
    ),
    *(
        FailureE2ECase(
            problem="isis_metric_style_mismatch",
            scenario="isp_arpanet196912",
            env_run_args=("--igp", "isis", "--device-profile", profile),
            isp_options={"igp": "isis", "bgp_mode": "none", "rpki": False},
            inject_seed=1,
        )
        for profile in ("iosxr", "routeros")
    ),
    FailureE2ECase(
        problem="isis_circuit_level_mismatch",
        scenario="isp_abilene",
        env_run_args=("--igp", "isis"),
        isp_options={"igp": "isis", "bgp_mode": "none", "rpki": False},
        inject_seed=1,
    ),
    *(
        FailureE2ECase(
            problem="isis_circuit_level_mismatch",
            scenario="isp_arpanet196912",
            env_run_args=("--igp", "isis", "--device-profile", profile),
            isp_options={"igp": "isis", "bgp_mode": "none", "rpki": False},
            inject_seed=1,
        )
        for profile in ("iosxr", "routeros")
    ),
    FailureE2ECase(
        problem="bmv2_switch_down",
        scenario="p4_dc_fabric",
        env_run_args=("-s", "s"),
        inject_seed=0,
        checks=frozenset({"verify", "symptom"}),
        sleep_after_inject_sec=1.0,
    ),
    FailureE2ECase(
        problem="host_static_blackhole",
        scenario="min3clos",
        env_run_args=(),
        topo_size="",
        inject_params={"host_name": "leaf1"},
        # StaticBlackHole has no recover_fault; assert verify + dataplane symptom.
        checks=frozenset({"verify", "symptom"}),
        sleep_after_inject_sec=2.0,
    ),
)


def _pipeline_mismatch_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="p4runtime_pipeline_mismatch",
            scenario="p4_dc_fabric",
            env_run_args=("-s", "s"),
            inject_seed=0,
            checks=frozenset({"verify", "symptom"}),
            sleep_after_inject_sec=2.0,
        )
    ]


def _all_e2e_cases() -> list[FailureE2ECase]:
    return [
        *CORE_E2E_CASES,
        *_link_detach_e2e_cases(),
        *_incast_e2e_cases(),
        *_pipeline_mismatch_e2e_cases(),
        *_flap_e2e_cases(),
    ]


# Licensed router images by ``--device-profile``.
_VENDOR_IMAGES = {
    "iosxr": "ios-xr/xrd-control-plane:26.2.1",
    "routeros": "vrnetlab/mikrotik_routeros:7.21.5",
}


def _device_profile(case: FailureE2ECase) -> str | None:
    args = list(case.env_run_args)
    if "--device-profile" in args:
        return args[args.index("--device-profile") + 1]
    standalone = case.scenario.removesuffix("_simple_bgp")
    return standalone if standalone in _VENDOR_IMAGES else None


def _case_id(case: FailureE2ECase) -> str:
    parts = [case.problem, case.scenario]
    if profile := _device_profile(case):
        parts.append(profile)
    if case.inject_seed != 1:
        parts.append(f"seed{case.inject_seed}")
    if case.topo_size not in ("", "s"):
        parts.append(f"topo-{case.topo_size}")
    return "-".join(parts)


def _skip_reason(case: FailureE2ECase) -> str | None:
    if not docker_available():
        return "docker required"
    if case.scenario == "enterprise_branch" and not linux_vrf_available():
        return "host kernel lacks Linux VRF"
    profile = _device_profile(case)
    if profile and not docker_image_available(_VENDOR_IMAGES[profile]):
        return f"{_VENDOR_IMAGES[profile]} image not installed"
    if case.scenario == "min3clos" and not containerlab_prerequisites():
        return "containerlab/gnmic not available"
    # Shared GHA runners: netns move for link_detach is unreliable.
    if case.problem == "link_detach" and artifact_verify_only():
        return "link_detach netns move unreliable under artifact CI"
    if case.problem != "link_flap":
        return None
    flap = FLAP_BY_SCENARIO.get(case.scenario)
    if flap is None:
        return None
    if flap.require_clab and not containerlab_prerequisites():
        return "containerlab/gnmic not available"
    if flap.require_privileged and not privileged_lab_supported():
        return "privileged k8s lab not supported"
    return None


@pytest.mark.parametrize("case", _all_e2e_cases(), ids=_case_id)
class TestFailureE2E(IntegrationTestCase):
    def _start_case_session(self, case: FailureE2ECase) -> str:
        """Start env; retry once for k3s labs (shared-runner k3s flake)."""
        attempts = 2 if case.scenario in {"k8s_lab", "llmd_lab"} else 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            session_id = None
            try:
                session_id = self._start_env(case.scenario, list(case.env_run_args))
                self._assert_session_ready(session_id, case.scenario)
                return session_id
            except BaseException as exc:
                last_exc = exc
                if session_id is not None:
                    with suppress(Exception):
                        self._close_session(session_id)
                if attempt + 1 >= attempts:
                    raise
        assert last_exc is not None
        raise last_exc

    def test_inject_verify_symptom_recover(self, case: FailureE2ECase) -> None:
        skip = _skip_reason(case)
        if skip:
            pytest.skip(skip)

        session_id = None
        try:
            session_id = self._start_case_session(case)
            run_failure_e2e(
                case,
                scenario_kwargs=self._scenario_kwargs(session_id),
            )
        finally:
            if session_id is not None:
                self._close_session(session_id)
