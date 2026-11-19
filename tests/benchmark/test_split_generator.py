"""Release contracts for joint network ownership and matched comparisons."""

from collections import Counter
from copy import deepcopy

import pytest

from nika.workflows.benchmark import split_generator as generator
from nika.workflows.benchmark.load_config import (
    load_benchmark_yaml,
    normalize_benchmark_row,
)
from nika.workflows.benchmark.network_identity import (
    DATASET_METADATA_FIELDS,
    dataset_row,
    network_identity,
    size_signature,
    twin_signature,
)
from nika.workflows.benchmark.release import (
    ReleaseError,
    _validate_dataset_slices,
    verify_dev_test_isolation,
)
from nika.workflows.benchmark.trials import index_rows_by_task_id, task_id_for_row

pytestmark = pytest.mark.contract


def _pool():
    rows = []
    for scenario, fault in (
        ("campus_lan", "link_down"),
        ("dc_clos", "link_flap"),
        ("enterprise_branch", "bgp_hijacking"),
    ):
        for size in ("s", "m", "l"):
            profiles = ("frr", "iosxr", "routeros") if size == "s" else ("frr",)
            for profile in profiles:
                for target in ("r1", "r2"):
                    rows.append(
                        {
                            "scenario": scenario,
                            "topo_size": size,
                            "problem": fault,
                            "device_profile": profile,
                            "inject": {"host_name": target, "intf_name": "eth0"},
                        }
                    )
                rows.append(
                    {
                        "scenario": scenario,
                        "topo_size": size,
                        "problem": "healthy",
                        "device_profile": profile,
                        "inject": {},
                    }
                )
            if scenario == "dc_clos" and size in {"m", "l"}:
                for target in ("r1", "r2"):
                    rows.append(
                        {
                            "scenario": scenario,
                            "topo_size": size,
                            "problem": fault,
                            "device_profile": "nokia_srlinux",
                            "backend": "containerlab",
                            "inject": {"host_name": target, "intf_name": "eth0"},
                        }
                    )
    return rows


@pytest.fixture
def small_inventory(monkeypatch):
    # Exercise the production solve with a small, complete authored inventory.
    monkeypatch.setattr(generator, "vm_memory_mib", lambda _: 24 * 1024)
    monkeypatch.setattr(generator, "HEALTHY_COUNT", 3)
    monkeypatch.setattr(generator, "THIRD_SIZE_COUNT", 3)
    monkeypatch.setattr(
        generator, "REQUIRED_SPLIT_FAMILIES", {"campus", "dc_clos", "enterprise_branch"}
    )
    monkeypatch.setattr(generator, "REQUIRED_SPLIT_SCALES", {"s", "m", "l"})
    monkeypatch.setattr(generator, "REQUIRED_SPLIT_BACKENDS", {"kathara"})
    monkeypatch.setattr(
        generator,
        "_benchmark_failures",
        lambda: {"link_down", "link_flap", "bgp_hijacking"},
    )
    return _pool()


def test_joint_core_twins_third_sizes_and_training_pool(small_inventory):
    datasets, report = generator.generate_splits(small_inventory)
    dev, test = datasets["dev"], datasets["test"]
    verify_dev_test_isolation(dev_cases=dev, test_cases=test)
    _validate_dataset_slices(dev, test)
    for fault in {r["problem"] for r in small_inventory} - {"healthy"}:
        pair = [r for r in dev + test if r["problem"] == fault and r["slice"] == "core"]
        assert len(pair) == 2
        assert sum(r in dev for r in pair) == 1
        assert len({r["topo_scale"] for r in pair}) == 2
        assert size_signature(pair[0]) == size_signature(pair[1])
        third = next(
            r for r in dev if r["problem"] == fault and r["slice"] == "third_size"
        )
        assert {r["topo_scale"] for r in [*pair, third]} == {"s", "m", "l"}
        assert set(third["core_task_ids"]) == {
            task_id_for_row(normalize_benchmark_row(r)) for r in pair
        }
    for rows in (dev, test):
        index_rows_by_task_id([normalize_benchmark_row(r) for r in rows])
        indexed = {task_id_for_row(normalize_benchmark_row(r)): r for r in rows}
        for twin in rows:
            if twin["slice"] == "vendor_twin":
                base = indexed[twin["base_task_id"]]
                assert base["slice"] == "core" and base["device_profile"] == "frr"
                assert twin_signature(twin) == twin_signature(base)
    # Native small-only twins coexist with different-sized cores and thirds.
    counts = Counter(
        r["device_profile"] for r in dev + test if r["slice"] == "vendor_twin"
    )
    # Each split needs all three core sizes, so only two of six cores can be s.
    assert counts == {"iosxr": 2, "routeros": 2, "nokia_srlinux": 1}
    test_networks = {network_identity(r) for r in test}
    assert not test_networks & {network_identity(r) for r in datasets["dev_pool"]}
    source = Counter(
        task_id_for_row(dataset_row(r))
        for r in small_inventory
        if report["network_assignment"][dataset_row(r)["network_id"]] == "dev"
    )
    assert Counter(task_id_for_row(r) for r in datasets["dev_pool"]) == source
    assert all(stage["optimal"] for stage in report["solver"]["stages"])


def test_memory_cap_applies_to_all_slices_and_dev_pool(small_inventory, monkeypatch):
    monkeypatch.setattr(
        generator,
        "vm_memory_mib",
        lambda variant: (
            39 * 1024
            if variant.get("device_profile") == "iosxr"
            and variant["scenario"] == "campus_lan"
            else 24 * 1024
        ),
    )
    datasets, report = generator.generate_splits(small_inventory)
    assert datasets["dev"] and datasets["test"] and datasets["dev_pool"]
    assert report["parameters"]["max_vm_memory_gib"] == 38
    assert report["excluded_vm_memory_task_ids"]
    assert all(
        not (row.get("device_profile") == "iosxr" and row["scenario"] == "campus_lan")
        for rows in datasets.values()
        for row in rows
    )


def test_deterministic_files_and_metadata_round_trip(
    small_inventory, tmp_path, monkeypatch
):
    monkeypatch.setattr(generator, "load_candidate_catalog", lambda _: small_inventory)
    a, b = tmp_path / "a", tmp_path / "b"
    generator.write_split_catalog(pool=tmp_path, output=a, seed=7)
    small_inventory.reverse()
    generator.write_split_catalog(pool=tmp_path, output=b, seed=7)
    for path in a.iterdir():
        assert path.read_bytes() == (b / path.name).read_bytes()
    plain = load_benchmark_yaml(a / "dev.yaml")
    assert all(not set(DATASET_METADATA_FIELDS) & r.keys() for r in plain)
    loaded = load_benchmark_yaml(a / "dev.yaml", include_metadata=True)
    assert any(r.get("core_task_ids") for r in loaded)


def test_combination_only_members_do_not_require_standalone_cores(small_inventory):
    from nika.problems.combinations import get_combination

    datasets, _ = generator.generate_splits(small_inventory)
    row = dataset_row(
        get_combination("prefix_surge_trips_low_limit").rows()[0],
        slice_name="multi_fault",
    )
    dev = [*datasets["dev"], row]
    _validate_dataset_slices(dev, datasets["test"])
    row.pop("schedule")
    row["problem"] = "link_down+dns_record_error"
    row["problems"] = ["link_down", "dns_record_error"]
    row["inject"] = {
        "link_down": {"host_name": "r1", "intf_name": "eth0"},
        "dns_record_error": {"host_name": "dns_server"},
    }
    row.update(dataset_row(row, slice_name="multi_fault"))
    with pytest.raises(ReleaseError, match="Missing core tasks.*dns_record_error"):
        _validate_dataset_slices(dev, datasets["test"])


def test_core_size_pair_minimum_share(small_inventory, monkeypatch):
    extra_faults = {
        "campus_lan": "host_missing_ip",
        "dc_clos": "ospf_interface_not_enabled",
        "enterprise_branch": "bgp_acl_block",
    }
    candidates = small_inventory + [
        {**row, "problem": extra_faults[row["scenario"]]}
        for row in small_inventory
        if row["problem"] != "healthy"
    ]
    monkeypatch.setattr(
        generator,
        "_benchmark_failures",
        lambda: {"link_down", "link_flap", "bgp_hijacking", *extra_faults.values()},
    )
    _, report = generator.generate_splits(candidates)
    assert report["quota_fallbacks"] == []
    assert report["size_patterns"] == {"s/m": 2, "s/l": 2, "m/l": 2}
    monkeypatch.setattr(generator, "CORE_SIZE_PAIR_MIN_SHARE", 0.34)
    _, infeasible = generator.generate_splits(candidates)
    assert not infeasible["hard_constraints"]["passed"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_base",
        "inject",
        "traffic",
        "telemetry",
        "objectives",
        "scenario",
        "network",
    ],
)
def test_reject_unmatched_twins_even_in_loaded_datasets(small_inventory, mutation):
    datasets, _ = generator.generate_splits(small_inventory)
    dev, test = deepcopy(datasets["dev"]), deepcopy(datasets["test"])
    twin = next(r for r in dev + test if r["slice"] == "vendor_twin")
    if mutation == "missing_base":
        twin.pop("base_task_id")
    elif mutation == "network":
        twin["topo_size"] = "l"
        twin.update(dataset_row(twin, slice_name="vendor_twin"))
    elif mutation == "scenario":
        twin["scenario"] = "isp_abilene"
        twin.update(dataset_row(twin, slice_name="vendor_twin"))
    else:
        twin[mutation] = {"changed": True}
    with pytest.raises(ReleaseError):
        _validate_dataset_slices(dev, test)


def test_reject_same_size_core_pair():
    pair = [
        dataset_row(
            {
                "scenario": scenario,
                "problem": "link_down",
                "inject": {"host_name": "r1"},
                "igp": "ospf",
                "bgp_mode": "none",
                "rpki": False,
                "backend": "kathara",
                "device_profile": "frr",
            }
        )
        for scenario in ("isp_abilene", "isp_dfn-bwin")
    ]
    assert {r["topo_scale"] for r in pair} == {"s"}
    with pytest.raises(ReleaseError, match="different sizes"):
        _validate_dataset_slices(pair[:1], pair[1:])


@pytest.mark.parametrize("scenario", ["dc_clos", "campus_lan", "k8s_lab", "llmd_lab"])
def test_all_variants_follow_resolved_network(scenario):
    from nika.net_env.net_env_pool import scenario_default_topo_size

    size = scenario_default_topo_size(scenario) or "s"
    if scenario_default_topo_size(scenario):
        assert network_identity({"scenario": scenario}) == network_identity(
            {"scenario": scenario, "topo_size": size}
        )
    dev = [
        {
            "scenario": scenario,
            "topo_size": size,
            "problem": "link_down",
            "inject": {"host_name": "a"},
        }
    ]
    test = [
        {
            "scenario": scenario,
            "topo_size": size,
            "problem": "link_down",
            "device_profile": "sonic",
            "backend": "containerlab",
            "inject": {"host_name": "b"},
        }
    ]
    with pytest.raises(ReleaseError, match="network isolation"):
        verify_dev_test_isolation(dev_cases=dev, test_cases=test)


def test_isp_protocol_variants_follow_base_graph():
    dev = [
        {
            "scenario": "isp_abilene",
            "problem": "link_down",
            "igp": "ospf",
            "inject": {"host_name": "a"},
        }
    ]
    test = [
        {
            "scenario": "isp_abilene",
            "problem": "link_down",
            "igp": "isis",
            "backend": "containerlab",
            "device_profile": "sonic",
            "inject": {"host_name": "b"},
        }
    ]
    with pytest.raises(ReleaseError, match="network isolation"):
        verify_dev_test_isolation(dev_cases=dev, test_cases=test)


def test_native_only_faults_belong_to_release_inventory():
    from nika.workflows.benchmark.release import _benchmark_failures

    assert {
        "bgp_address_family_not_activated",
        "bgp_session_policy_missing",
    } <= _benchmark_failures()
