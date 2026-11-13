"""DNS record edits preserve file metadata and reach the running name server."""

import subprocess

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_prepared_failure, prepare_failure
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not docker_available(), reason="Docker required for DNS")
def test_record_error_serves_changed_zone_with_original_mtime(monkeypatch):
    session_id = start_net_env(
        "campus_lan", "s", session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    failure = None
    runtime = None
    try:
        session = Session().load_running_session(session_id=session_id)
        runtime = runtime_for_session(session)
        params = {
            "host_name": "dns_server",
            "target_domain": "local",
            "target_website": "web0",
            "wrong_ip": "192.0.2.77",
        }
        stat = "stat -c '%y' /etc/bind/db.local"
        query = "dig +short +time=2 +tries=1 web0.local"
        before = runtime.exec("dns_server", stat)
        healthy = runtime.exec("pc_1_1_1_1", query)
        assert healthy.strip() and "192.0.2.77" not in healthy
        failure = prepare_failure(session, ["dns_record_error"], params)
        inject_prepared_failure(session, ["dns_record_error"], failure)
        assert runtime.exec("dns_server", stat) == before
        assert runtime.exec("pc_1_1_1_1", query).strip() == "192.0.2.77"
        assert failure.recover()["verified"]
        failure = None
        assert runtime.exec("pc_1_1_1_1", query) == healthy
    finally:
        try:
            if failure is not None:
                failure.recover()
        finally:
            subprocess.run(
                [
                    "uv",
                    "run",
                    "nika",
                    "session",
                    "close",
                    "--session_id",
                    session_id,
                    "--yes",
                ],
                check=True,
                timeout=180,
            )
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
