"""Benchmark cases per router device profile (offline, no Docker)."""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from nika.net_env.net_env_pool import deployment_needs_licensed_images
from nika.problems.registry import get_problem_class, list_avail_problem_instances
from nika.problems.support.router_ops import (
    failure_supports_profile,
    profile_fault_operations,
)
from nika.workflows.benchmark.generate import (
    _isp_stack_variants,
    build_failure_cases,
    iter_failure_case_specs,
    native_benchmark_profiles,
)
from nika.workflows.benchmark.load_config import normalize_benchmark_row

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_vrf_import_mismatch_requires_routeros_forwarding(profile):
    cases, _ = build_failure_cases(
        problem="vrf_route_target_mismatch",
        scenario="enterprise_branch",
        topo_size="s",
        isp_options={"device_profile": profile},
    )
    assert bool(cases) is (profile == "routeros")


@pytest.mark.parametrize(
    ("scenario", "profile", "bgp_mode", "supported"),
    [
        ("dc_clos", "frr", None, False),
        ("dc_clos", "iosxr", None, False),
        ("dc_clos", "routeros", None, True),
        ("enterprise_branch", "iosxr", None, False),
        ("isp_arpanet196912", "routeros", "ebgp", True),
    ],
)
def test_ipv4_family_cases_require_a_platform_that_keeps_bgp_established(
    scenario, profile, bgp_mode, supported
):
    options = {"device_profile": profile}
    if bgp_mode is not None:
        options.update(igp="ospf", bgp_mode=bgp_mode, rpki=False)
    cases, _ = build_failure_cases(
        problem="bgp_address_family_not_activated",
        scenario=scenario,
        topo_size="s",
        isp_options=options,
    )
    assert bool(cases) is supported


def test_compatibility_follows_the_vendor_fault_operations() -> None:
    # A failure needs every operation its native path uses; profiles offer
    # the operations their provider implements.
    assert {"bgp_asn", "set_bgp_asn"} <= profile_fault_operations("iosxr")
    asn = get_problem_class("bgp_asn_misconfig")
    assert failure_supports_profile(asn, "iosxr")
    assert failure_supports_profile(asn, "routeros") == (
        {"bgp_asn", "set_bgp_asn"} <= profile_fault_operations("routeros")
    )
    # Acting on the container interface needs no router operation.
    assert failure_supports_profile(get_problem_class("link_down"), "routeros")
    # TC on the container veth; RouterOS CHR still crosses it.
    blackhole = get_problem_class("device_flow_blackhole")
    assert blackhole.runs_on_device_profile("routeros")
    assert not blackhole.runs_on_device_profile("iosxr")
    # XRd drops the adjacency instead of staying Full but unrouted.
    mismatch = get_problem_class("ospf_network_type_mismatch")
    assert mismatch.runs_on_device_profile("routeros")
    assert not mismatch.runs_on_device_profile("iosxr")
    # Without a native implementation a failure stays on FRR/Linux nodes.
    frr_down = get_problem_class("frr_service_down")
    assert not failure_supports_profile(frr_down, "iosxr")
    # OSPF filters, OSPF interfaces and detached links run on every platform.
    for failure in ("ospf_acl_block", "ospf_neighbor_missing", "link_detach"):
        for profile in ("iosxr", "routeros", "sonic"):
            assert failure_supports_profile(get_problem_class(failure), profile)


def test_generation_expands_native_profiles_only_at_size_s() -> None:
    assert native_benchmark_profiles("dc_clos", "s") == ["iosxr", "routeros"]
    assert native_benchmark_profiles("dc_clos", "m") == []
    assert native_benchmark_profiles("campus_lan", "l") == []
    assert native_benchmark_profiles("isp_abilene") == ["iosxr", "routeros"]
    assert native_benchmark_profiles("isp_geant") == []  # topology tier m
    assert native_benchmark_profiles("isp_abilene_ebgp_rpki") == []
    specs = {
        (scenario, size, (options or {}).get("device_profile"))
        for problem, scenario, size, options in iter_failure_case_specs()
        if problem == "link_down" and scenario == "dc_clos"
    }
    assert specs == {
        ("dc_clos", "s", None),
        ("dc_clos", "s", "iosxr"),
        ("dc_clos", "s", "routeros"),
        ("dc_clos", "m", None),
        ("dc_clos", "l", None),
    }


def test_targets_avoid_native_routers_that_cannot_run_the_failure() -> None:
    # dc_clos converts every router: frr_service_down has no XR target.
    cases, rejected = build_failure_cases(
        problem="frr_service_down",
        scenario="dc_clos",
        topo_size="s",
        isp_options={"device_profile": "iosxr"},
    )
    assert not cases
    assert {row["reason"] for row in rejected} == {"unsupported_device_profile"}
    # campus_lan converts its core: the OSPF binding fault also targets XR.
    cases, _ = build_failure_cases(
        problem="ospf_area_misconfiguration",
        scenario="campus_lan",
        topo_size="s",
        isp_options={"device_profile": "iosxr"},
    )
    targets = {case["inject"]["host_name"] for case in cases}
    assert any(name.startswith("router_core") for name in targets)
    # A native implementation targets the XR routers, named as NIKA nodes.
    cases, _ = build_failure_cases(
        problem="bgp_asn_misconfig",
        scenario="dc_clos",
        topo_size="s",
        isp_options={"device_profile": "iosxr"},
    )
    assert cases and all(case["device_profile"] == "iosxr" for case in cases)
    assert cases[0]["root_causes"] == [
        {
            "resource": {"kind": "node", "node": cases[0]["inject"]["host_name"]},
            "fault_type": "bgp_asn_misconfig",
        }
    ]
    cases, rejected = build_failure_cases(
        problem="ospf_network_type_mismatch",
        scenario="isp_abilene",
        topo_size="s",
        isp_options={
            "igp": "ospf",
            "bgp_mode": "none",
            "rpki": False,
            "device_profile": "iosxr",
        },
    )
    assert not cases
    assert {row["reason"] for row in rejected} == {"unsupported_device_profile"}


def test_native_rows_parse_and_need_licensed_images() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos",
            "problem": "link_down",
            "topo_size": "s",
            "device_profile": "routeros",
            "inject": {"host_name": "leaf_router_0_0", "intf_name": "eth0"},
        }
    )
    assert row["device_profile"] == "routeros"
    with pytest.raises(ValueError):
        normalize_benchmark_row(
            {
                "scenario": "sdn_l3_clos",
                "problem": "link_down",
                "topo_size": "s",
                "device_profile": "iosxr",
                "inject": {"host_name": "h1", "intf_name": "eth0"},
            }
        )
    assert deployment_needs_licensed_images("dc_clos", "iosxr")
    assert not deployment_needs_licensed_images("dc_clos")
    assert deployment_needs_licensed_images("routeros_simple_bgp")


def test_bgp_md5_targets_originate_paths_in_route_reflector_plans() -> None:
    from nika.net_env.isp.bgp import compile_bgp_plan
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan

    plan = compile_bgp_plan(
        compile_isp_plan(IspConfig(topology="abilene", igp="isis")), "ibgp_rr"
    )
    originators = {item.device for item in plan.originated}
    for profile in ("frr", "iosxr", "routeros"):
        cases, rejected = build_failure_cases(
            problem="bgp_md5_password_mismatch",
            scenario="isp_abilene",
            topo_size="s",
            isp_options={
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "device_profile": profile,
            },
        )
        assert {case["inject"]["host_name"] for case in cases} == originators
        assert all(row["inject"]["host_name"] not in originators for row in rejected)


def _native_operations_used(problem_cls) -> set[str]:
    """Operations a failure class requests through the native dispatch."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(problem_cls)))
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "id", None) == "native_fault_ops" and len(node.args) >= 3:
            used.add(node.args[2].value)
        if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "ops":
            used.add(func.attr)
    return used


def test_failures_declare_every_native_operation_they_use() -> None:
    for name, problem_cls in list_avail_problem_instances().items():
        used = _native_operations_used(problem_cls)
        declared = problem_cls.native_router_operations
        if used:
            assert declared is not None and used <= declared, (name, used, declared)


def test_predicate_has_a_device_profile_dimension() -> None:
    link_down = get_problem_class("link_down")
    assert link_down.is_compatible("dc_clos", "iosxr")
    assert not link_down.is_compatible("sdn_l3_clos", "iosxr")
    assert link_down.runs_on_device_profile("iosxr")
    frr_down = get_problem_class("frr_service_down")
    # The deployment is compatible; its XR routers are not targets.
    assert frr_down.is_compatible("campus_lan", "iosxr")
    assert not frr_down.runs_on_device_profile("iosxr")
    # A Containerlab platform runs a failure through its provider (SONiC);
    # SR Linux needs the failure's declared Containerlab implementation.
    max_prefix = get_problem_class("bgp_max_prefix_exceeded")
    assert max_prefix.is_compatible("isp_abilene/ebgp", "sonic")
    assert not max_prefix.is_compatible("isp_abilene/ebgp", "nokia_srlinux")
    assert not max_prefix.is_compatible("isp_abilene/ebgp", backend="containerlab")
    assert max_prefix.is_compatible("isp_abilene/ebgp", backend="kathara")
    assert not link_down.is_compatible("dc_clos", "sonic")
    # SONiC routers run FRR even where the scenario's default routers do not.
    assert frr_down.is_compatible("min3clos", "sonic")
    assert not frr_down.is_compatible("min3clos")


@pytest.mark.parametrize(
    "failure", ["mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"]
)
def test_pmtu_failures_run_where_routers_forward_in_linux(failure: str) -> None:
    cls = get_problem_class(failure)
    # SONiC forwards in the kernel on every ISP it deploys; SR Linux does not.
    assert cls.is_compatible("isp_dfn-bwin", "sonic")
    assert not cls.is_compatible("isp_dfn-bwin", "nokia_srlinux")
    assert not cls.is_compatible("isp_dfn-bwin", "frr")
    assert cls.is_compatible("isp_abilene", "frr")
    stacks = {
        (scenario, (options or {}).get("device_profile"))
        for problem, scenario, _size, options in iter_failure_case_specs()
        if problem == failure and scenario == "isp_dfn-bwin"
    }
    assert stacks == {("isp_dfn-bwin", "sonic")}


def test_validation_results_round_trip_and_key_isp_as_one_environment(
    tmp_path,
) -> None:
    from nika.workflows.benchmark.profile_validation import (
        VALIDATED,
        cell_environment,
        load_results,
        validated_cells,
        write_results,
    )

    assert cell_environment("isp_polska") == "isp"
    assert cell_environment("dc_clos") == "dc_clos"
    path = tmp_path / "device_profiles.yaml"
    record = {
        "environment": "dc_clos",
        "device_profile": "iosxr",
        "failure": "bgp_asn_misconfig",
        "status": VALIDATED,
    }
    write_results({"dc_clos/iosxr/bgp_asn_misconfig": record}, path)
    assert load_results(path) == {"dc_clos/iosxr/bgp_asn_misconfig": record}
    assert validated_cells(path) == {"dc_clos/iosxr/bgp_asn_misconfig"}


def test_profile_validation_covers_containerlab_provider_platforms(
    tmp_path, monkeypatch
) -> None:
    import yaml

    from nika.workflows.benchmark import generate
    from nika.workflows.benchmark import profile_validation as validation

    root_causes = [
        {
            "resource": {"kind": "node", "node": "leaf1"},
            "fault_type": "bgp_asn_misconfig",
        }
    ]
    cases = [
        {"inject": {"host_name": "leaf1"}, "root_causes": root_causes},
        {
            "device_profile": "sonic",
            "inject": {"host_name": "leaf1"},
            "root_causes": root_causes,
        },
    ]
    (tmp_path / "min3clos").mkdir()
    (tmp_path / "min3clos" / "bgp_asn_misconfig.yaml").write_text(
        yaml.safe_dump(
            {
                "scenario": {"name": "min3clos"},
                "failure": {"fault_type": "bgp_asn_misconfig", "cases": cases},
            }
        )
    )
    cells = validation.declared_cells(tmp_path)
    assert [(c.key, c.native_target) for c in cells] == [
        ("min3clos/sonic/bgp_asn_misconfig", True)
    ]
    assert cells[0].row["device_profile"] == "sonic"
    assert validation.profile_image("sonic").startswith("docker-sonic-vs:")

    # --validated-only drops SONiC rows of unvalidated cells, keeps SR Linux.
    monkeypatch.setattr(validation, "validated_cells", lambda: set())
    monkeypatch.setattr(
        generate,
        "iter_failure_case_specs",
        lambda: iter(
            [
                ("bgp_asn_misconfig", "min3clos", "", None),
                ("bgp_asn_misconfig", "min3clos", "", {"device_profile": "sonic"}),
            ]
        ),
    )
    built = []
    monkeypatch.setattr(
        generate,
        "build_failure_cases",
        lambda **kwargs: (built.append(kwargs["isp_options"]), ([], []))[1],
    )
    generate.build_candidate_catalog(validated_only=True)
    assert built == [None]


def test_runs_select_rows_by_platform_and_name_missing_licensed_images(
    monkeypatch,
) -> None:
    from nika.net_env.utils.kathara.docker_files import docker_images
    from nika.workflows.benchmark.licensed import (
        require_licensed_images,
        select_rows,
    )

    native = {"scenario": "dc_clos", "device_profile": "routeros"}
    licensed = {"scenario": "routeros_simple_bgp"}
    frr = {"scenario": "dc_clos"}
    rows = [native, licensed, frr]
    assert select_rows(rows, skip_licensed=True) == [frr]
    assert select_rows(rows, device_profiles=["routeros"]) == [native, licensed]
    assert select_rows(rows, device_profiles=["frr"]) == [frr]
    srl = {"scenario": "min3clos"}
    sonic = {"scenario": "min3clos", "device_profile": "sonic"}
    isp_srl = {"scenario": "isp_pdh", "backend": "containerlab"}
    platform_rows = [srl, sonic, isp_srl, frr]
    assert select_rows(platform_rows, device_profiles=["sonic"]) == [sonic]
    assert select_rows(platform_rows, device_profiles=["nokia_srlinux"]) == [
        srl,
        isp_srl,
    ]
    monkeypatch.setattr(docker_images, "image_exists", lambda image: False)
    with pytest.raises(ValueError, match="--skip-licensed") as exc:
        require_licensed_images(rows)
    assert "dc_clos/routeros x1" in str(exc.value)
    assert "routeros_simple_bgp/routeros x1" in str(exc.value)
    require_licensed_images([frr])


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
@pytest.mark.parametrize("method", ["inject_fault", "verify_fault"])
def test_generic_guard_refuses_native_targets_before_running_commands(profile, method):
    from types import SimpleNamespace

    from nika.problems.base import ProblemBase
    from nika.runtime.base import RuntimeCapabilityError

    class FrrOnlyFailure(ProblemBase):
        def inject_fault(self, params):
            return "executed"

        def verify_fault(self, params):
            return "executed"

    failure = object.__new__(FrrOnlyFailure)
    failure.net_env = SimpleNamespace(
        machine_identities={
            "native": SimpleNamespace(capabilities=(profile,)),
            "frr": SimpleNamespace(capabilities=("frr",)),
        }
    )
    with pytest.raises(RuntimeCapabilityError, match="native routers"):
        getattr(failure, method)(params={"host_name": "native"})
    assert getattr(failure, method)({"host_name": "frr"}) == "executed"


def test_profile_validation_resumes_and_preserves_other_writers(tmp_path, monkeypatch):
    import fcntl
    from contextlib import contextmanager

    from nika.net_env.utils.kathara.docker_files import docker_images
    from nika.workflows.benchmark import profile_validation as validation
    from nika.workflows.benchmark.trials import task_id_for_row

    path = tmp_path / "results.yaml"
    lock_path = tmp_path / "lab.lock"
    row = {
        "scenario": "dc_clos",
        "problem": "link_down",
        "device_profile": "routeros",
        "inject": {"host_name": "r1", "intf_name": "eth0"},
    }
    cell = validation.Cell("dc_clos", "routeros", "link_down", row, True)
    calls = []

    def run_cell(current, **kwargs):
        with lock_path.open("a") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(current)
        return {
            "environment": current.environment,
            "device_profile": current.device_profile,
            "failure": current.failure,
            "task_id": task_id_for_row(current.row),
            "status": "validated",
            "duration_sec": 1,
        }

    original_lock = validation.heavy_lab_lock

    @contextmanager
    def competing_writer():
        # Simulate a validator completing another cell before we acquire the lock.
        with original_lock(lock_path):
            records = validation.load_results(path)
            records["dc_clos/iosxr/link_down"] = {
                "environment": "dc_clos",
                "device_profile": "iosxr",
                "failure": "link_down",
                "status": "validated",
            }
            validation.write_results(records, path)
            yield

    monkeypatch.setattr(validation, "heavy_lab_lock", competing_writer)
    monkeypatch.setattr(validation, "run_cell", run_cell)
    monkeypatch.setattr(docker_images, "image_exists", lambda image: True)
    validation.validate_cells([cell], results_path=path, echo=lambda message: None)
    validation.validate_cells([cell], results_path=path, echo=lambda message: None)
    assert len(calls) == 1
    assert "dc_clos/iosxr/link_down" in validation.load_results(path)
    changed = validation.Cell(
        cell.environment,
        cell.device_profile,
        cell.failure,
        {**row, "inject": {"host_name": "r2", "intf_name": "eth0"}},
        True,
    )
    validation.validate_cells([changed], results_path=path, echo=lambda message: None)
    assert len(calls) == 2


def test_failure_compatibility_requires_a_shared_backend() -> None:
    # bgp_hijacking runs on Kathara and on SONiC's provider, not on SR Linux.
    hijack = get_problem_class("bgp_hijacking")
    assert hijack.is_compatible("enterprise_branch")
    assert not hijack.is_compatible("min3clos")
    assert hijack.is_compatible("isp_abilene", "sonic", backend="containerlab")
    # Multi-backend scenarios retain a usable Kathara deployment.
    assert hijack.is_compatible("isp_abilene")
    assert hijack.is_compatible("isp_abilene", backend="kathara")
    assert not hijack.is_compatible("isp_abilene", backend="containerlab")
    specs = list(iter_failure_case_specs())
    assert not any(
        problem == "bgp_hijacking" and scenario == "min3clos"
        for problem, scenario, *_ in specs
    )
    # A link failure applied outside the nodes runs on both backends.
    corruption = get_problem_class("link_packet_corruption")
    assert corruption.is_compatible("min3clos", "sonic", backend="containerlab")
    assert corruption.is_compatible("isp_abilene", backend="containerlab")


def test_forwarding_corruption_runs_on_containerlab_wire_ends() -> None:
    corruption = get_problem_class("device_forwarding_packet_corruption")
    # The classifier sits on the host-side wire end, so any Containerlab
    # router platform carries it.
    assert corruption.is_compatible("min3clos", "nokia_srlinux")
    assert corruption.is_compatible("min3clos", "sonic")
    assert corruption.runs_on_device_profile("sonic")
    # On Kathara it loads in the node's Linux namespace, which IOS XR and
    # RouterOS do not forward through.
    assert not corruption.runs_on_device_profile("iosxr")
    assert not corruption.runs_on_device_profile("routeros")


def test_containerlab_isp_size_follows_platform_memory() -> None:
    from nika.workflows.benchmark.e2e_validation import containerlab_isp_supported

    # SR Linux keeps graphs of up to 11 routers; SONiC routers need less memory.
    assert containerlab_isp_supported("isp_pdh", "nokia_srlinux")
    assert not containerlab_isp_supported("isp_abilene", "nokia_srlinux")
    assert containerlab_isp_supported("isp_abilene", "sonic")
    assert containerlab_isp_supported("isp_geant", "sonic")
    assert not containerlab_isp_supported("isp_brain", "sonic")
    variants = _isp_stack_variants(
        "isp_geant", get_problem_class("bgp_max_prefix_exceeded")
    )
    assert {"backend": "containerlab", "device_profile": "sonic"} in variants
    assert all(v["device_profile"] != "nokia_srlinux" for v in variants)


def test_compatibility_obeys_declared_path_bdp_ceiling() -> None:
    from nika.net_env.net_env_pool import scenario_path_bdp_limit_bytes
    from nika.problems.base import ProblemBase

    class NeedsLargeBdp(ProblemBase):
        minimum_path_bdp_bytes = 65536

    assert scenario_path_bdp_limit_bytes("enterprise_branch", "routeros") == 10000
    assert not get_problem_class("tcp_receive_window_limited").is_compatible(
        "enterprise_branch", "routeros"
    )
    assert not NeedsLargeBdp.is_compatible("enterprise_branch", "routeros")
    assert NeedsLargeBdp.is_compatible("enterprise_branch", "frr")
    assert NeedsLargeBdp.is_compatible("enterprise_branch", "iosxr")
    assert NeedsLargeBdp.is_compatible("enterprise_branch")
    assert scenario_path_bdp_limit_bytes("dc_clos", "routeros") is None
    NeedsLargeBdp.minimum_path_bdp_bytes = 10000
    assert NeedsLargeBdp.is_compatible("enterprise_branch", "routeros")


def test_containerlab_catalog_task_id_round_trip() -> None:
    from nika.workflows.benchmark.generate import default_pool_dir
    from nika.workflows.benchmark.trials import task_id_for_row
    from nika.workflows.case import load_example_cases

    catalog = default_pool_dir() / "min3clos" / "host_static_blackhole.yaml"
    rows = load_example_cases(catalog)
    assert rows
    row = rows[0]
    assert load_example_cases(catalog, task_id=task_id_for_row(row)) == [row]


def test_compatibility_obeys_declared_path_rate() -> None:
    receiver = get_problem_class("receiver_resource_contention")
    sender = get_problem_class("sender_resource_contention")
    # Every RouterOS path is slower than a CPU-starved reader.
    assert not receiver.is_compatible("dc_clos", "routeros")
    assert receiver.is_compatible("dc_clos", "iosxr")
    # The 1 Mbit/s RouterOS fabric keeps a quota-held sender's effect; the
    # enterprise overlay shrinks best effort below it.
    assert sender.is_compatible("dc_clos", "routeros")
    assert not sender.is_compatible("enterprise_branch", "routeros")
    # TCP sustains about 1 Mbit/s even under heavy corruption.
    assert not get_problem_class("link_packet_corruption").is_compatible(
        "dc_clos", "routeros"
    )


def test_every_environment_baseline_profile_parses() -> None:
    from nika.net_env.net_env_pool import list_all_net_envs, scenario_baseline_traffic
    from nika.traffic.profiles import normalize_traffic

    for scenario in list_all_net_envs():
        assert normalize_traffic(scenario_baseline_traffic(scenario)), scenario


def test_faulty_and_healthy_cases_run_the_same_baseline() -> None:
    from nika.workflows.benchmark.generate import _healthy_specs

    baseline = {"pattern": "uniform", "load_mbps": 1.0}
    for problem in ("link_down", "link_flap"):
        cases, _ = build_failure_cases(
            problem=problem, scenario="dc_clos", topo_size="s", isp_options=None
        )
        assert cases and all(case["traffic"] == baseline for case in cases)
    document = {
        "scenario": {"name": "dc_clos"},
        "failure": {"fault_type": "link_down", "cases": cases},
    }
    specs = [spec for spec in _healthy_specs([document]) if spec[0] == "dc_clos"]
    assert specs and all(options["traffic"] == baseline for _, _, options in specs)


def test_public_service_link_is_observed_from_a_guest_host() -> None:
    cases, _ = build_failure_cases(
        problem="link_packet_corruption",
        scenario="enterprise_branch",
        topo_size="s",
        isp_options=None,
    )
    row = next(c for c in cases if c["inject"]["host_name"] == "internet_web")
    assert row["inject"]["observer_device"].endswith("_guest_pc")
    assert row["inject"]["probe_dst_ip"] == "198.51.100.10"


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_duplicate_ospf_id_targets_have_stub_subnets(profile):
    cases, rejected = build_failure_cases(
        problem="ospf_duplicate_router_id",
        scenario="campus_lan",
        topo_size="s",
        isp_options={"device_profile": profile},
    )
    assert {case["inject"]["host_name"] for case in cases} == {
        "router_dist_1_1",
        "router_dist_2_1",
        "server_access_router",
    }
    assert {row["inject"]["host_name"] for row in rejected} == {
        "router_core_1",
        "router_core_2",
        "router_core_3",
    }
    cases, rejected = build_failure_cases(
        problem="ospf_duplicate_router_id",
        scenario="isp_abilene",
        topo_size="s",
        isp_options={
            "device_profile": profile,
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": False,
        },
    )
    assert cases and not rejected
    assert failure_supports_profile(
        get_problem_class("ospf_duplicate_router_id"), profile
    )


def test_bgp_ttl_security_requires_a_reproducible_native_mismatch():
    problem = get_problem_class("bgp_ttl_security_mismatch")
    assert failure_supports_profile(problem, "iosxr")
    assert not failure_supports_profile(problem, "routeros")
    for profile in ("iosxr", "routeros"):
        cases, rejected = build_failure_cases(
            problem="bgp_ttl_security_mismatch",
            scenario="dc_clos",
            topo_size="s",
            isp_options={"device_profile": profile},
        )
        if profile == "iosxr":
            assert cases and not rejected
        else:
            assert not cases
            assert {row["reason"] for row in rejected} == {"unsupported_device_profile"}


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_ospf_timer_mismatch_requires_a_host_serving_cut_edge(profile):
    from types import SimpleNamespace

    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.problems.routing_control_plane.ospf import ospf_router_links
    from nika.problems.rca.materialize import ground_truth_for_case

    problem = get_problem_class("ospf_hello_interval_mismatch")
    env = get_net_env_instance("campus_lan", topo_size="s", device_profile=profile)
    ctx = SimpleNamespace(net_env=env, scenario="campus_lan", topo_size="s")
    valid = {"host_name": "router_core_1", "intf_name": "eth2"}
    problem.validate_benchmark_inject(ctx, valid)
    truth = ground_truth_for_case(
        problem=problem.root_cause_name,
        params=valid,
        scenario="campus_lan",
        topo_size="s",
        net_env=env,
    )
    assert [cause.resource.id for cause in truth.root_causes] == ["node/router_core_1"]
    if profile != "frr":
        assert failure_supports_profile(problem, profile)
    for ends in ospf_router_links(env):
        if all(node.startswith("router_core_") for node, _ in ends):
            host, intf = ends[0]
            with pytest.raises(ValueError, match="cut edge serving hosts"):
                problem.validate_benchmark_inject(
                    ctx, {"host_name": host, "intf_name": intf}
                )
            break
    else:
        pytest.fail("campus topology has no redundant core link")


def test_ospf_timer_mismatch_reports_an_environment_without_a_cut_edge():
    cases, rejected = build_failure_cases(
        problem="ospf_hello_interval_mismatch",
        scenario="isp_france",
        topo_size="s",
        isp_options={"igp": "ospf", "bgp_mode": "none", "rpki": False},
    )
    assert not cases
    assert {row["reason"] for row in rejected} == {"case_validation_failed"}
    assert all("cut edge serving hosts" in row["error"] for row in rejected)
