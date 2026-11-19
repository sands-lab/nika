"""Slow HTTP recovery signals only its recorded process identity."""

import subprocess
from pathlib import Path

import pytest

from nika.problems.security.web import WebDoS, WebDoSParams

pytestmark = pytest.mark.unit


class LocalRuntime:
    def exec(self, node, command, **kwargs):
        return subprocess.run(
            command, shell=True, check=True, capture_output=True, text=True
        ).stdout


def test_slow_client_owns_process_and_rejects_reused_pid():
    params = WebDoSParams(host_name="web", attacker_device="client", slow_connections=1)
    owned, legitimate = WebDoS(None), WebDoS(None)
    owned.runtime = legitimate.runtime = LocalRuntime()
    identities = {}
    try:
        for client in (owned, legitimate):
            client._slow_process = client._slow_process_action(
                params, "start", "127.0.0.1", 1
            )
            identities[client] = client._slow_process.copy()
        assert owned._slow_client_count(params) == 1
        pid = owned._slow_process[0]
        argv = Path(f"/proc/{pid}/cmdline").read_bytes()
        assert argv.startswith(b"http-client\x00")
        assert b"socket" not in argv and b"-c\x00" not in argv
        identity = owned._slow_process.copy()
        owned._slow_process[1] += 1
        assert owned._slow_client_count(params) == 0
        owned._slow_process_action(params, "stop")
        owned._slow_process = identity
        assert owned._slow_client_count(params) == 1
        owned._slow_process_action(params, "stop")
        assert owned._slow_client_count(params) == 0
        assert legitimate._slow_client_count(params) == 1
    finally:
        for client, identity in identities.items():
            client._slow_process = identity
            client._slow_process_action(params, "stop")
