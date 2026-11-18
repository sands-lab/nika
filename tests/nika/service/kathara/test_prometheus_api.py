"""Contracts for the Prometheus ``prometheus_query`` service API."""

from __future__ import annotations

import json
import shlex

import pytest

from nika.service.kathara.prometheus_api import PrometheusAPIMixin

pytestmark = pytest.mark.unit


class _FakeCollector(PrometheusAPIMixin):
    def __init__(self, response: dict | str) -> None:
        self.response = response if isinstance(response, str) else json.dumps(response)
        self.commands: list[str] = []

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        assert host_name == "telemetry_collector"
        self.commands.append(command)
        return self.response


def _curl_params(api: _FakeCollector) -> tuple[str, dict[str, str]]:
    argv = shlex.split(api.commands[-1])
    url = next(arg for arg in argv if arg.startswith("http://"))
    params = dict(
        argv[i + 1].split("=", 1)
        for i, arg in enumerate(argv)
        if arg == "--data-urlencode"
    )
    return url, params


def _vector(*series: tuple[dict, str]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": metric, "value": [1760000000.0, value]}
                for metric, value in series
            ],
        },
    }


def test_instant_query_names_the_node_and_drops_scrape_labels() -> None:
    api = _FakeCollector(
        _vector(
            (
                {
                    "__name__": "node_network_up",
                    "device": "eth0",
                    "instance": "172.28.0.3:9100",
                    "job": "node",
                    "node": "chinng",
                },
                "0",
            )
        )
    )
    rows = api.prometheus_query('node_network_up{device="eth0"}')

    assert rows == [
        {
            "node": "chinng",
            "metric": {"__name__": "node_network_up", "device": "eth0"},
            "value": 0.0,
        }
    ]
    url, params = _curl_params(api)
    assert url.endswith("/api/v1/query")
    assert params == {"query": 'node_network_up{device="eth0"}'}


def test_range_query_returns_every_point() -> None:
    values = [[1760000000 + i, str(i)] for i in range(100)]
    api = _FakeCollector(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {"node": "iplsng"}, "values": values}],
            },
        }
    )
    rows = api.prometheus_query("up", start_time="-600", step="30s")

    assert len(rows[0]["values"]) == 100
    assert rows[0]["values"][-1] == [1760000099, 99.0]
    url, params = _curl_params(api)
    assert url.endswith("/api/v1/query_range")
    assert set(params) == {"query", "start", "end", "step"}
    assert params["step"] == "30s"


def test_query_stays_one_curl_argument() -> None:
    api = _FakeCollector(_vector())
    hostile = "up' ; rm -rf / ; echo '"

    assert api.prometheus_query(hostile) == []
    assert _curl_params(api)[1]["query"] == hostile


def test_limit_returns_the_first_series_only() -> None:
    api = _FakeCollector(_vector(*(({"node": f"r{i}"}, "1") for i in range(150))))
    rows = api.prometheus_query("up", limit=120)

    assert [row["node"] for row in rows] == [f"r{i}" for i in range(120)]
    assert len(api.prometheus_query("up", limit=500)) == 150


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": ""}, "query must be"),
        ({"query": "up", "step": "fast"}, "step must be"),
        ({"query": "up", "limit": 0}, "limit must be"),
    ],
)
def test_invalid_arguments_are_rejected(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _FakeCollector(_vector()).prometheus_query(**kwargs)


def test_prometheus_errors_are_reported() -> None:
    api = _FakeCollector(
        {"status": "error", "errorType": "bad_data", "error": "parse error"}
    )
    with pytest.raises(ValueError, match="parse error"):
        api.prometheus_query("up{")
