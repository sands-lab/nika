"""The Clos web service retains reference evidence within its memory budget."""

import asyncio
import importlib
import json
import shutil

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from scripts.benchmark_audit import live
from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.net_env.verify import http_ok
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration

_RESOURCE_READ = """python3 - <<'PY'
import json, pathlib, subprocess
c = pathlib.Path('/sys/fs/cgroup')
events = dict(line.split() for line in (c/'memory.events').read_text().splitlines())
servers = []
for p in pathlib.Path('/proc').glob('[0-9]*'):
    try:
        argv = (p/'cmdline').read_bytes().split(b'\\0')
        if len(argv) > 1 and argv[1].endswith(b'/static-' + b'httpd.py'):
            fields = dict(line.split(':', 1) for line in (p/'status').read_text().splitlines() if ':' in line)
            servers.append({'pid': int(p.name), 'threads': int(fields['Threads'])})
    except (FileNotFoundError, ProcessLookupError):
        pass
print(json.dumps({'events': {k: int(v) for k, v in events.items()},
    'current': int((c/'memory.current').read_text()),
    'maximum': (c/'memory.max').read_text().strip(), 'servers': servers,
    'listener': subprocess.check_output(['ss', '-Hln', 'sport', '=', ':80'], text=True).strip()}))
PY"""


@pytest.mark.skipif(
    not docker_available() or not shutil.which("clab"),
    reason="Requires Docker and Containerlab",
)
def test_clos_http_capacity_preserves_reference_and_recovery(tmp_path, monkeypatch):
    params = {
        "attacker_device": "client_4_2",
        "host_name": "web_2",
        "observer_device": "client_1_1",
        "probe_url": "http://10.0.2.11/",
    }
    row = {
        "scenario": "dc_clos",
        "topo_size": "s",
        "backend": "containerlab",
        "device_profile": "sonic",
        "telemetry": ["netflow", "pingmesh", "prometheus", "syslog"],
        "traffic": {"pattern": "uniform", "load_mbps": 1.0},
        "problem": "web_dos_attack",
        "inject": params,
    }
    sid = start_net_env(
        row["scenario"],
        row["topo_size"],
        **{k: row[k] for k in ("backend", "device_profile", "telemetry", "traffic")},
        result_dir=tmp_path / "sessions",
    )
    records = []
    prepared_failures = []
    result = runtime = None
    late_reference = []
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)

        async def read_resources(phase):
            client = MultiServerMCPClient(
                connections=load_session_mcp_config(sid, "dc_clos", phase="diagnosis")
            )
            tools = {t.name: t for t in await client.get_tools()}
            observed = {}
            for host in ("web_2", "web_1"):
                raw = await tools["exec_shell"].ainvoke(
                    {"host_name": host, "command": _RESOURCE_READ, "timeout": 15}
                )
                observed[host] = json.loads(tool_text(raw))
            records.append({"phase": phase, "hosts": observed})
            for host, data in observed.items():
                assert data["listener"] and data["servers"], (host, data)
                assert max(s["threads"] for s in data["servers"]) <= 129, data
                assert data["maximum"] == "268435456", data
                assert data["current"] < int(data["maximum"]), data
                assert data["events"]["oom_kill"] == 0, data
                assert data["events"]["oom"] == 0, data

        def measure(phase):
            with engine_gateway(sid, session.session_dir, GatewayRequest()):
                asyncio.run(read_resources(phase))

        measure("healthy")
        original_oracle = live._oracle_stage
        original_artifact = live._artifact_stage

        def retain_prepared(*args, **kwargs):
            prepared = prepare_failure(*args, **kwargs)
            prepared_failures.append(prepared)
            return prepared

        def oracle(*args, **kwargs):
            stage = original_oracle(*args, **kwargs)
            measure("injected_reference")
            return stage

        def artifact(label, raw):
            if label == "final_artifact":
                late_reference.append(original_oracle(sid))
                measure("late_reference")
            return original_artifact(label, raw)

        monkeypatch.setattr(live, "_oracle_stage", oracle)
        monkeypatch.setattr(live, "_artifact_stage", artifact)
        monkeypatch.setattr(
            importlib.import_module("nika.workflows.failure.inject"),
            "prepare_failure",
            retain_prepared,
        )
        result = live.audit_open_session(sid, row)
        assert result.admission() == "pass", result.model_dump()
        assert len(late_reference) == 1 and late_reference[0].status == "pass", (
            late_reference
        )
        assert len(prepared_failures) == 1
        assert prepared_failures[0].recover()["verified"]
        assert http_ok(runtime, params["observer_device"], params["probe_url"])
        measure("recovered")
    finally:
        (tmp_path / "http-capacity.json").write_text(
            json.dumps(
                {
                    "session": sid,
                    "resources": records,
                    "audit": result.model_dump(mode="json") if result else None,
                    "late_reference": [
                        s.model_dump(mode="json") for s in late_reference
                    ],
                },
                indent=2,
            )
        )
        close_session(session_id=sid)
        if runtime is not None:
            assert not runtime.exists()
            assert not runtime.has_leftover_resources()
