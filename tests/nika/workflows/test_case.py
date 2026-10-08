"""Manual case launch through the CLI, real fault symptoms, and recovery."""

import re
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nika.cli.main import app
from nika.config import BENCHMARK_DIR
from nika.utils.session import Session
from nika.utils.session_store import SessionStore
from nika.validation.presence import bound_injected_problem
from nika.workflows.case import load_example_cases
from nika.workflows.benchmark.trials import task_id_for_row
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]


def test_manual_case_launch_and_recovery(tmp_path: Path) -> None:
    import docker

    catalog = BENCHMARK_DIR / "working" / "pool" / "dc_clos" / "link_down.yaml"
    row = next(
        row
        for row in load_example_cases(catalog)
        if row["topo_size"] == "s"
        and row["inject"] == {"host_name": "client_0", "intf_name": "eth0"}
    )
    runner = CliRunner()
    session_id = None
    lab_name = None
    try:
        launched = runner.invoke(
            app,
            [
                "case",
                "run",
                task_id_for_row(row),
                "--catalog",
                str(catalog),
                "--result-dir",
                str(tmp_path),
            ],
        )
        match = re.search(r"^session_id=(\S+)$", launched.output, re.MULTILINE)
        if match:
            session_id = match.group(1)
        assert launched.exit_code == 0, launched.output
        assert session_id is not None, launched.output
        session = Session()
        session.load_running_session(session_id=session_id)
        lab_name = session.lab_name
        bound = bound_injected_problem(session_id)
        assert bound is not None
        problem, params = bound
        assert problem.verify_fault(params=params)["verified"]
        down = runner.invoke(
            app,
            [
                "exec",
                "--session_id",
                session_id,
                "client_0",
                "cat /sys/class/net/eth0/operstate",
            ],
        )
        assert down.exit_code == 0, down.output
        assert down.output.strip() == "down"
        endpoints = row["root_causes"][0]["resource"]["name"].split("--")
        peer = next(
            endpoint for endpoint in endpoints if not endpoint.startswith("client_0:")
        )
        peer_host, peer_intf = peer.split(":", 1)
        destination = problem.runtime.get_host_ip(peer_host, peer_intf)
        assert destination
        ping_args = [
            "exec",
            "--session_id",
            session_id,
            "client_0",
            f"ping -c 1 -W 1 {destination}",
        ]
        failed_ping = runner.invoke(app, ping_args)
        assert (
            "100% packet loss" in failed_ping.output
            or "Network is unreachable" in failed_ping.output
        )
        assert problem.recover_fault(params=params)["verified"]
        restored = runner.invoke(app, ping_args)
        assert restored.exit_code == 0, restored.output
        assert (
            "0% packet loss" in restored.output
            and "100% packet loss" not in restored.output
        )
    finally:
        if session_id:
            close_session(session_id=session_id)
    assert session_id not in {
        item["session_id"] for item in SessionStore().list_running_sessions()
    }
    with closing(docker.from_env()) as client:
        assert not any(
            lab_name in container.name for container in client.containers.list(all=True)
        )
