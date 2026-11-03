"""IS-IS route reads separate a failed query from an empty route table."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.validation.symptom.routing_probes import _isis_routes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("[TIMEOUT] command expired", None),
        ("", set()),
        ("10.0.0.0/24 via 10.1.1.1 dev eth0 proto isis metric 20\n", {"10.0.0.0/24"}),
    ],
)
def test_linux_isis_routes(output, expected):
    problem = SimpleNamespace(runtime=SimpleNamespace(exec=lambda *a, **k: output))
    assert _isis_routes(problem, "r1") == expected


def test_linux_isis_routes_exec_error_is_unreadable():
    def fail(*args, **kwargs):
        raise RuntimeError("container gone")

    problem = SimpleNamespace(runtime=SimpleNamespace(exec=fail))
    assert _isis_routes(problem, "r1") is None


def test_stub_baseline_requires_every_observers_route_read(monkeypatch):
    from nika.validation.symptom import routing_probes as probes

    problem = SimpleNamespace(_audit_isis_stubs={"stale": True})
    monkeypatch.setattr(
        probes,
        "adjacency_baseline",
        lambda *_: (True, {"adjacencies_up": ["isis|r2|eth0"]}),
    )
    monkeypatch.setattr(probes, "lab_routers", lambda *_: ["r1", "r2", "r3"])
    monkeypatch.setattr(
        probes,
        "_planned_stubs",
        lambda _, node: {"10.0.0.0/24"} if node == "r1" else set(),
    )
    monkeypatch.setattr(
        probes,
        "_isis_routes",
        lambda _, node: None if node == "r3" else {"10.0.0.0/24"},
    )
    ok, details = probes.isis_stub_baseline(problem, SimpleNamespace(host_name="r1"))
    assert not ok
    assert details == {"error": "isis_route_query_failed", "routers": ["r3"]}
    assert problem._audit_isis_stubs is None


@pytest.mark.parametrize("status", [0, 255])
def test_route_query_separates_empty_output_from_silent_transport_failure(status):
    import subprocess

    from nika.validation.symptom.routing_probes import _route_query

    def execute(node, command, **kwargs):
        return subprocess.run(
            ["sh", "-c", command], text=True, capture_output=True, check=True
        ).stdout

    runtime = SimpleNamespace(exec=execute)
    result = _route_query(runtime, "r1", f"exit {status}", timeout=1)
    assert result == (None if status else "")
