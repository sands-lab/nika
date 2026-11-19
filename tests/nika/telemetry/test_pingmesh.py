"""Endpoint inventory, topology grouping, rotation, and expected isolation."""

import math

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.telemetry import pingmesh
from nika.telemetry.pingmesh_targets import TARGET_BUDGET, all_targets, target_batch

pytestmark = pytest.mark.unit


def test_endpoints_come_from_inventory_and_groups_from_topology() -> None:
    env = get_net_env_instance("dc_clos", backend="kathara", topo_size="s")
    plan = pingmesh.topology_plan(env)
    assert plan["endpoints"]
    for name, endpoint in plan["endpoints"].items():
        identity = env.machine_identities[name]
        assert (
            identity.role.value in {"host", "service"} or identity.reachability_target
        )
        assert endpoint["interfaces"]
        assert any(endpoint["group"] in str(link) for link in env.get_topology())


def test_rotating_mesh_covers_all_pairs_and_respects_probe_budget() -> None:
    endpoints = {
        f"arbitrary_{i}": {
            "address": f"10.0.0.{i + 1}",
            "management": f"172.28.0.{i + 1}",
            "group": str(i // 5),
            "service": None,
        }
        for i in range(30)
    }
    targets = all_targets(endpoints, [[["arbitrary_0"], ["arbitrary_1"]]])
    assert len(targets) == 30 * 29 * 2
    seen = set()
    for turn in range(math.ceil(len(targets) / TARGET_BUDGET)):
        batch = target_batch(targets, turn)
        assert len(batch) <= TARGET_BUDGET
        for row in batch:
            labels = row["labels"]
            seen.add((labels["source"], labels["target"], labels["module"]))
            assert row["targets"][0].endswith(":9115")
            assert labels["__param_target"].startswith("10.0.0.")
    assert len(seen) == len(targets)
    isolated = [
        r
        for r in targets
        if r["labels"]["source"] == "arbitrary_0"
        and r["labels"]["target"] == "arbitrary_1"
    ]
    assert all(r["labels"]["expected"] == "isolated" for r in isolated)
    assert target_batch(targets, 0) == target_batch(
        targets, math.ceil(len(targets) / TARGET_BUDGET)
    )


def test_declared_service_modules_and_minimum_interval() -> None:
    endpoints = {
        "a": {
            "address": "10.0.0.1",
            "management": "172.28.0.1",
            "group": "r",
            "service": None,
        },
        "b": {
            "address": "10.0.0.2",
            "management": "172.28.0.2",
            "group": "r",
            "service": "web",
        },
    }
    rows = all_targets(endpoints, [])
    assert {r["labels"]["module"] for r in rows} == {"tcp_connect", "icmp", "http"}
    assert pingmesh.scrape_job()["scrape_interval"] == "15s"
    assert pingmesh.scrape_job()["scrape_timeout"] == "14s"
    assert "file_sd_configs" not in pingmesh.scrape_job()
    assert pingmesh.scrape_job()["http_sd_configs"][0]["url"].endswith(":9103/targets")
    assert pingmesh.MODULES["tcp_connect"]["timeout"] == "12s"


def test_collectors_share_host_probe_budget_and_release_it(tmp_path) -> None:
    import fcntl
    from nika.telemetry.pingmesh_targets import host_budget

    first = tmp_path / "a.lease"
    second = tmp_path / "b.lease"
    with first.open("w") as a, second.open("w") as b:
        fcntl.flock(a, fcntl.LOCK_EX)
        fcntl.flock(b, fcntl.LOCK_EX)
        assert host_budget(first) + host_budget(second) == TARGET_BUDGET
        assert host_budget(first) == TARGET_BUDGET // 2
        fcntl.flock(b, fcntl.LOCK_UN)
        assert host_budget(first) == TARGET_BUDGET


def test_kubernetes_probers_use_host_network_cached_images_and_matching_mounts() -> (
    None
):
    import base64
    import json
    import shlex
    from types import SimpleNamespace

    calls = []
    runtime = SimpleNamespace(
        exec=lambda node, command, timeout: calls.append((node, command))
    )
    pingmesh.start_kubernetes_probers(
        runtime,
        "control",
        {"endpoint": {"address": "10.0.0.1", "pod_image": "cached:workload"}},
    )
    manifest = json.loads(base64.b64decode(shlex.split(calls[0][1])[2]))
    spec = manifest["items"][0]["spec"]
    assert spec["hostNetwork"] and spec["nodeName"] == "endpoint"
    assert spec["containers"][0]["image"].startswith(
        "prom/blackbox-exporter:v0.23.0@sha256:"
    )
    assert all(c["imagePullPolicy"] == "Never" for c in spec["containers"])
    responder = spec["containers"][1]
    assert responder["command"][1] == responder["volumeMounts"][0]["mountPath"]
    assert responder["volumeMounts"][0]["name"] in {v["name"] for v in spec["volumes"]}


def test_dns_responsiveness_accepts_authoritative_refusal_and_dual_service():
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance("dc_clos", topo_size="s")
    plan = pingmesh.topology_plan(env)
    assert plan["endpoints"]["web_1"]["service"] == "dns"
    assert plan["endpoints"]["web_1"]["http"]
    dns = pingmesh.MODULES["dns"]["dns"]
    assert "REFUSED" in dns["valid_rcodes"]
    assert "SERVFAIL" not in dns["valid_rcodes"]


def test_readiness_checks_exporter_http_health_on_minimal_nodes(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(pingmesh, "process_running", lambda *_: True)
    calls = []

    def execute(node, command, timeout):
        calls.append((node, command))
        if node == "collector":
            if "/api/v1/targets" in command:
                return '{"data":{"activeTargets":[{"labels":{"job":"pingmesh"},"health":"up"}]}}'
            return "probe_budget 128"
        return "200\n" if "urllib.request" in command else "pgrep: not found"

    runtime = SimpleNamespace(exec=execute)
    assert pingmesh.running(runtime, "collector", {"worker": "172.28.0.1"})
    assert any("/-/healthy" in command for _, command in calls)
    runtime.exec = lambda *_args, **_kwargs: (
        '{"data":{"activeTargets":[{"labels":{"job":"pingmesh"},"health":"down"}]}}'
    )
    assert not pingmesh._scrapes_running(runtime, "collector")
    runtime.exec = lambda *_args, **_kwargs: ""
    assert not pingmesh.running(runtime, "collector", {"worker": "172.28.0.1"})


@pytest.mark.parametrize(
    "health,ready",
    [
        (["unknown"], False),
        (["up", "unknown"], True),
        (["up", "down", "unknown"], False),
    ],
)
def test_readiness_distinguishes_rotated_targets_from_failed_scrapes(health, ready):
    import json
    from types import SimpleNamespace

    targets = [{"labels": {"job": "pingmesh"}, "health": state} for state in health]
    runtime = SimpleNamespace(
        exec=lambda *_args, **_kwargs: json.dumps({"data": {"activeTargets": targets}})
    )
    assert pingmesh._scrapes_running(runtime, "collector") is ready
