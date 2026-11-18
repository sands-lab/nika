"""Declared application rates used to size ingress policies."""

from types import SimpleNamespace

import pytest

from nika.problems.service_networking.ingress import workload_routes
from nika.traffic.workload import BusinessWorkload, WorkloadProcess

pytestmark = pytest.mark.unit


def test_each_worker_calls_each_route_before_waiting():
    workload = BusinessWorkload(
        processes=(
            WorkloadProcess(
                node="client",
                app="api_client",
                command="web-user",
                args=(
                    "http://app.test/word",
                    "http://app.test/weather?location=London",
                    "--workers",
                    "2",
                    "--every",
                    "3-9",
                ),
            ),
        )
    )
    routes = workload_routes(SimpleNamespace(business_workload=lambda: workload))
    assert routes[("app.test", "/word")] == (
        "client",
        "http://app.test/word",
        pytest.approx(13.3333333),
    )
    assert routes[("app.test", "/weather")] == (
        "client",
        "http://app.test/weather?location=London",
        pytest.approx(13.3333333),
    )
