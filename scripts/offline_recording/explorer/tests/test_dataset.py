"""Dataset parsing tests against a small synthetic dataset folder."""

import json

import pytest
from fastapi.testclient import TestClient

from explorer_backend.app import create_app
from explorer_backend.dataset import (
    Dataset,
    counter_rate,
    parse_series_name,
    parse_time_ms,
    sonic_port_name,
)

pytestmark = pytest.mark.unit
TID = "lab-0001"


@pytest.fixture
def dataset_dir(tmp_path):
    task = tmp_path / "tasks" / TID
    (task / "devices" / "r1").mkdir(parents=True)
    (task / "devices" / "r1" / "routes.txt").write_text("10.0.0.0/24 via 10.0.1.2\n")
    (tmp_path / "before_fault" / TID / "devices" / "r1").mkdir(parents=True)
    (tmp_path / "before_fault" / TID / "devices" / "r1" / "routes.txt").write_text(
        "10.0.0.0/24 via 10.0.1.1\n"
    )
    (task / "task.json").write_text(
        json.dumps(
            {
                "task_id": TID,
                "network": "lab",
                "description": "desc",
                "telemetry": ["prometheus", "syslog", "netflow"],
                "devices": [
                    {"name": "r1", "role": "router", "capabilities": ["frr"]},
                    {"name": "h1", "role": "host", "capabilities": []},
                    {"name": "h2", "role": "host", "capabilities": []},
                ],
                "segments": [
                    [
                        {"device": "r1", "port": "eth1"},
                        {"device": "h1", "port": "eth0"},
                    ],
                    [
                        {"device": "r1", "port": "eth2"},
                        {"device": "h1", "port": "eth1"},
                        {"device": "h2", "port": "eth0"},
                    ],
                ],
            }
        )
    )
    (task / "timeline.json").write_text(
        json.dumps(
            {
                "recording_start": "2026-01-01T00:00:00+00:00",
                "alert_time": "2026-01-01T00:00:02+00:00",
                "state_snapshot_time": "2026-01-01T00:00:03+00:00",
                "recording_end": "2026-01-01T00:00:04+00:00",
            }
        )
    )
    prom = task / "telemetry" / "prometheus"
    prom.mkdir(parents=True)
    (prom / "node_network_receive_bytes_total.csv").write_text(
        "time,r1{device=eth1},h1{device=eth0}\n"
        "2026-01-01T00:00:00Z,100,5\n"
        "2026-01-01T00:00:01Z,300,\n"
        "2026-01-01T00:00:02Z,50,7\n"
        "2026-01-01T00:00:03Z,150,9\n"
    )
    (prom / "node_load1.csv").write_text("time,r1,h1\n2026-01-01T00:00:00Z,0.5,0.1\n")
    (prom / "sonic_interface_in_octets.csv").write_text(
        "time,s1{device=Ethernet4}\n2026-01-01T00:00:00Z,0\n2026-01-01T00:00:01Z,10\n"
    )
    (task / "telemetry" / "syslog.log").write_text(
        "2026-01-01T00:00:02.000Z r1 bgpd: neighbor 10.0.0.2 Down\n2026-01-01T00:00:01.000Z h1 kernel: link up\n"
    )
    (task / "telemetry" / "netflow.csv").write_text(
        "first_seen,last_seen,exporter,in_if,proto,src_ip,src_port,dst_ip,dst_port,packets,bytes,flows\n"
        "2026-01-01 00:00:00.000,2026-01-01 00:00:02.000,r1,1,tcp,10.0.0.1,1234,10.0.0.2,80,10,1000,1\n"
        "2026-01-01 00:00:01.000,2026-01-01 00:00:01.500,r1,1,tcp,10.0.0.1,1235,10.0.0.2,80,5,500,1\n"
        "2026-01-01 00:00:01.000,2026-01-01 00:00:01.500,h1,1,udp,10.0.0.3,53,10.0.0.1,999,1,90,1\n"
    )
    probes = task / "probes"
    probes.mkdir()
    rounds = [
        {
            "time": "2026-01-01T00:00:00+00:00",
            "summary": {
                "reachable_pairs": 1,
                "total_pairs": 1,
                "rtt_ms": {"median": 1.0},
            },
            "endpoints": {"h1": "10.0.0.1"},
            "results": [
                {
                    "source": "h1",
                    "target": "h2",
                    "loss_percent": 0.0,
                    "rtt_avg_ms": 1.0,
                    "status": "ok",
                }
            ],
        },
        {
            "time": "2026-01-01T00:00:03+00:00",
            "summary": {"reachable_pairs": 0, "total_pairs": 1},
            "results": [
                {
                    "source": "h1",
                    "target": "h2",
                    "loss_percent": 100.0,
                    "rtt_avg_ms": None,
                    "status": "unreachable",
                }
            ],
        },
    ]
    (probes / "pingmesh.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rounds) + "\n"
    )
    (probes / "http_dns.jsonl").write_text(
        json.dumps(
            {
                "time": "2026-01-01T00:00:01+00:00",
                "kind": "http",
                "source": "h1",
                "target": "h2",
                "code": "000",
                "total": "2.5",
            }
        )
        + "\n"
        + json.dumps(
            {
                "time": "2026-01-01T00:00:01+00:00",
                "kind": "dns",
                "source": "h1",
                "name": "a.local",
                "status": "NOERROR",
                "query_ms": 3,
                "answers": ["1.2.3.4"],
            }
        )
        + "\n"
    )
    int_dir = task / "telemetry" / "int"
    int_dir.mkdir()
    hop = {"switch_id": 7, "hop_latency": 2000, "queue_occupancy": 3}
    (int_dir / "reports.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "packet_timestamp": ts,
                    "src": "a",
                    "dst": "b",
                    "protocol": 6,
                    "dst_port": 80,
                    "hop_sequence": [hop],
                }
            )
            for ts in (1_000_000_000, 1_500_000_000, 3_000_000_000)
        )
        + "\n"
    )
    (tmp_path / "labels").mkdir()
    (tmp_path / "labels" / f"{TID}.json").write_text(
        json.dumps(
            {
                "is_anomaly": True,
                "root_causes": [{"fault_type": "x", "component": "node/r1"}],
                "fault_injected_at": "2026-01-01T00:00:01+00:00",
            }
        )
    )
    return tmp_path


def test_helpers():
    assert parse_series_name("leaf_1{device=eth0,mode=x}") == (
        "leaf_1",
        {"device": "eth0", "mode": "x"},
    )
    assert parse_series_name("r1") == ("r1", {})
    assert parse_time_ms("2026-01-01 00:00:01.500") == parse_time_ms(
        "2026-01-01T00:00:01.5Z"
    )
    assert counter_rate([0, 1000, 2000, 4000], [10, 20, 5, 25]) == [
        None,
        10.0,
        None,
        10.0,
    ]
    assert (
        sonic_port_name("eth1") == "Ethernet0"
        and sonic_port_name("eth3") == "Ethernet8"
        and sonic_port_name("eth0") is None
    )


def test_task_detail(dataset_dir):
    task = Dataset(dataset_dir).task(TID)
    detail = task.detail()
    assert [s["kind"] for s in detail["segments"]] == ["link", "lan"]
    r1 = next(d for d in detail["devices"] if d["name"] == "r1")
    assert (
        r1["views"] == ["routes"]
        and r1["before_fault_views"] == ["routes"]
        and r1["has_metrics"]
    )
    assert detail["collectors"]["int"] and not detail["collectors"]["iperf"]
    assert not detail["has_topology_png"]
    assert (
        detail["timeline"]["alert_time_ms"] - detail["timeline"]["recording_start_ms"]
        == 2000
    )
    assert (
        task.label()["fault_injected_at_ms"]
        == detail["timeline"]["recording_start_ms"] + 1000
    )


def test_metrics_and_interfaces(dataset_dir):
    task = Dataset(dataset_dir).task(TID)
    kinds = {m["metric"]: m["kind"] for m in task.metrics()}
    assert kinds == {
        "node_load1": "gauge",
        "node_network_receive_bytes_total": "counter",
        "sonic_interface_in_octets": "counter",
    }
    series = task.metric_series("node_network_receive_bytes_total", ["r1"])
    assert series["rate"] and [s["id"] for s in series["series"]] == ["r1{device=eth1}"]
    assert series["series"][0]["values"] == [None, 200.0, None, 100.0]
    raw = task.metric_series("node_network_receive_bytes_total", ["h1"], rate="false")
    assert raw["series"][0]["values"] == [5.0, None, 7.0, 9.0]
    assert task.interface("r1", "eth1")["counters"]["rx_bytes"][1] == 200.0
    sonic = task.interface("s1", "eth2")
    assert sonic["source"] == "sonic" and sonic["counters"]["rx_bytes"] == [None, 10.0]
    assert task.interface("h2", "eth0")["source"] is None


def test_logs_flows_probes_int(dataset_dir):
    task = Dataset(dataset_dir).task(TID)
    assert [r["device"] for r in task.syslog_rows] == ["h1", "r1"]
    assert task.syslog_rows[1]["program"] == "bgpd"
    flows = task.netflow(["r1"], None, None, None, 10)
    assert flows["matched"] == 2 and flows["top_talkers"][0]["bytes"] == 1500
    assert sum(sum(s["values"]) for s in flows["timeseries"]["series"]) * flows[
        "timeseries"
    ]["bucket_s"] == pytest.approx(1500)
    mesh = task.pingmesh()
    assert (
        mesh["pairs"][0]["status"] == ["ok", "unreachable"]
        and mesh["summary"][0]["rtt_median"] == 1.0
    )
    http, dns = task.http_dns()["rows"]
    assert (http["ok"], http["latency_ms"], dns["ok"], dns["target"]) == (
        False,
        2500.0,
        True,
        "a.local",
    )
    summary = task.int_summary
    assert summary["n_reports"] == 3 and len(summary["t"]) == 3
    assert (
        summary["switches"][0]["reports"] == [2, 0, 1]
        and summary["switches"][0]["hop_latency_avg_us"][0] == 2.0
    )
    assert task.int_records(1, 5)["rows"][0]["packet_timestamp"] == 1_500_000_000


def test_api_rejects_traversal(dataset_dir):
    client = TestClient(create_app(dataset_dir, static_dir=dataset_dir / "missing"))
    assert client.get("/api/tasks").json()[0]["n_segments"] == 2
    assert (
        client.get(
            f"/api/tasks/{TID}/devices/r1/views/routes",
            params={"phase": "before_fault"},
        )
        .json()["text"]
        .endswith("10.0.1.1\n")
    )
    assert (
        client.get(
            f"/api/tasks/{TID}/devices/..%2F..%2F..%2Fetc/views/passwd"
        ).status_code
        == 404
    )
    assert client.get(f"/api/tasks/{TID}/controllers/%2E%2E").status_code == 404
    assert client.get("/api/tasks/../labels").status_code == 404
