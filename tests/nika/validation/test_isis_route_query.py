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
