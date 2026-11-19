"""DNS record edits preserve file metadata and reach the running name server."""

import subprocess
import shlex
import json

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
@pytest.mark.parametrize("scenario", ["campus_lan", "dc_clos"])
def test_record_error_serves_changed_zone_with_original_mtime(monkeypatch, scenario):
    session_id = start_net_env(
        scenario, "s", session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    failure = None
    runtime = None
    try:
        session = Session().load_running_session(session_id=session_id)
        runtime = runtime_for_session(session)
        host, domain, website, client = (
            ("dns_server", "local", "web0", "pc_1_1_1_1")
            if scenario == "campus_lan"
            else ("web_1", "fabric", "web-3", "client_1_1")
        )
        params = {
            "host_name": host,
            "target_domain": domain,
            "target_website": website,
            "wrong_ip": "192.0.2.77",
        }
        stat = f"stat -c '%y' /etc/bind/db.{domain}"
        query = f"dig +short +time=2 +tries=1 {website}.{domain}"
        before = runtime.exec(host, stat)
        healthy = runtime.exec(client, query)
        metadata_command = "python3 -c " + shlex.quote(
            "import glob,json,os; print(json.dumps({p:[os.stat(p).st_atime_ns,os.stat(p).st_mtime_ns,os.stat(p).st_ctime_ns] for p in glob.glob('/etc/bind/*') if os.path.isfile(p) and not os.path.islink(p)}))"
        )
        metadata_before = json.loads(runtime.exec(host, metadata_command))
        assert healthy.strip() and "192.0.2.77" not in healthy
        failure = prepare_failure(session, ["dns_record_error"], params)
        inject_prepared_failure(session, ["dns_record_error"], failure)
        assert runtime.exec(host, stat) == before
        assert runtime.exec(client, query).strip() == "192.0.2.77"
        metadata_injected = json.loads(runtime.exec(host, metadata_command))
        assert {p: times[1] for p, times in metadata_injected.items()} == {
            p: times[1] for p, times in metadata_before.items()
        }
        assert (
            max(t[2] for t in metadata_injected.values())
            - min(t[2] for t in metadata_injected.values())
            < 5_000_000_000
        )
        assert failure.recover()["verified"]
        failure = None
        metadata_recovered = json.loads(runtime.exec(host, metadata_command))
        assert {p: times[1] for p, times in metadata_recovered.items()} == {
            p: times[1] for p, times in metadata_before.items()
        }
        assert (
            max(t[2] for t in metadata_recovered.values())
            - min(t[2] for t in metadata_recovered.values())
            < 5_000_000_000
        )
        assert runtime.exec(client, query) == healthy
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
