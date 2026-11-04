"""Run complete task files through the benchmark CLI and check their timelines."""

import json
import subprocess

import pytest
import yaml

from tests.support.prerequisites import docker_available
from tests.support.schedule_assertions import assert_schedule_timing

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_benchmark_timing_for_complete_task_files_and_execution_defaults(tmp_path):
    from nika.runtime.factory import runtime_for_session
    from nika.workflows.session.close import close_session

    link = {"host_name": "client_0", "intf_name": "eth0"}
    dns = {
        "host_name": "dns_pod0",
        "target_domain": "pod0",
        "target_website": "web0",
        "wrong_ip": "203.0.113.99",
    }
    burst = {
        "type": "burst",
        "sources": ["dns_pod0"],
        "destination": "webserver0_pod0",
        "protocol": "udp",
        "rate": "100K",
    }
    cases = {
        "healthy": {"problem": "healthy"},
        "initial-link": {"problem": "link_down", "inject": link},
        "initial-multiple": {
            "problems": ["link_down", "dns_record_error"],
            "inject": {"link_down": link, "dns_record_error": dns},
        },
        "same-failure-targets": {
            "schedule": {
                "events": [
                    {"problem": "link_down", "inject": link},
                    {
                        "problem": "link_down",
                        "inject": {"host_name": "dns_pod0", "intf_name": "eth0"},
                    },
                ]
            }
        },
        "early-agent": {
            "traffic": {"pattern": "incast", "incast_period_sec": 30},
            "schedule": {
                "agent_start": 0,
                "events": [
                    {"traffic": "background", "at": 3},
                    {"traffic": "business", "at": 6},
                    {
                        "traffic": burst,
                        "at": 8,
                        "duration": 3,
                        "repeat": {"every": 8, "count": 2},
                    },
                    {
                        "problem": "link_down",
                        "inject": link,
                        "at": 9,
                        "duration": 4,
                        "repeat": {"every": 12, "count": 2},
                    },
                    {"problem": "dns_record_error", "inject": dns, "at": 12},
                ],
            },
        },
        "delayed-agent": {
            "schedule": {
                "agent_start": 10,
                "events": [
                    {"problem": "link_down", "inject": link, "at": 1, "duration": 5},
                    {
                        "problem": "dns_record_error",
                        "inject": dns,
                        "at": 9,
                        "duration": 6,
                        "repeat": {"every": 10, "count": 2},
                    },
                ],
            }
        },
        "initial-schedule": {
            "schedule": {
                "events": [
                    {"problem": "link_down", "inject": link},
                    {"problem": "dns_record_error", "inject": dns},
                    {"traffic": {**burst, "protocol": "tcp"}, "at": 8, "duration": 3},
                ],
            }
        },
    }
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    for name, case in cases.items():
        (task_dir / f"{name}.yaml").write_text(
            yaml.safe_dump({"scenario": "dc_clos", "topo_size": "s", **case})
        )
    config = tmp_path / "benchmark.yaml"
    config.write_text(
        yaml.safe_dump({"cases": [f"tasks/{name}.yaml" for name in cases]})
    )
    results = tmp_path / "results"
    with (tmp_path / "benchmark.log").open("w") as log:
        process = subprocess.Popen(
            [
                "uv",
                "run",
                "nika",
                "benchmark",
                "run",
                "--config",
                str(config),
                "--agent",
                "mock",
                "--batch-size",
                "2",
                "--case-timeout",
                "120",
                "--result_dir",
                str(results),
                "--yes",
                "--output-mode",
                "agent",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            assert process.wait(timeout=360) == 0, (
                tmp_path / "benchmark.log"
            ).read_text()
            trials = list(results.glob("trials/*"))
            assert len(trials) == len(cases)
            for directory in trials:
                run = json.loads((directory / "run.json").read_text())
                assert run["status"] == "finished" and run["outcome"] == "success", run
                assert_schedule_timing(directory, agent=True)
                metrics = json.loads((directory / "eval_metrics.json").read_text())
                assert metrics["detection_score"] == metrics["rca_f1"] == 1
                assert metrics["tool_errors"] == 0
                truth = json.loads((directory / "ground_truth.json").read_text())
                for root in truth["root_causes"]:
                    resource = root["resource"]
                    if root["fault_type"] == "link_down":
                        assert resource["kind"] == "link"
                        assert any(
                            target in resource["name"]
                            for target in ("client_0:eth0", "dns_pod0:eth0")
                        )
                    else:
                        assert root["fault_type"] == "dns_record_error"
                        assert resource["node"] == "dns_pod0"
                runtime = runtime_for_session(run)
                assert not runtime.exists() and not runtime.has_leftover_resources()
                assert not (directory / "schedule-owner.json").exists()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=90)
            for path in results.glob("trials/*/run.json"):
                run = json.loads(path.read_text())
                if run.get("session_id"):
                    close_session(
                        session_id=run["session_id"],
                        session_dir=str(path.parent),
                        status=run.get("status", "aborted"),
                    )
