"""Measure capture startup postconditions in an isolated, pinned Linux image."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from auditlib.probe import ProbePlan
from auditlib.source import read_source, source_files


class CapturePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    commit: str
    source_path: str
    interface: str

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        return ProbePlan.pinned_image(value)

    @field_validator("source_path")
    @classmethod
    def production_source_path(cls, value: str) -> str:
        return ProbePlan.source_path_inside_code(value)

    @field_validator("interface")
    @classmethod
    def native_invalid_name(cls, value: str) -> str:
        if len(value) > 64 or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+", value
        ):
            raise ValueError(
                "probe needs an observed slash-delimited native interface name"
            )
        return value


_WORKER = r"""
import ast
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import types
import uuid
import __future__
from pathlib import Path

source = Path('/probe/source.py').read_bytes().decode()
plan = json.loads(Path('/probe/plan.json').read_text())
anchor = '    if not pid.isdigit():\n'
patch = '''
    if pid.isdigit():
        ready = runtime.exec(device, f"sleep 0.2; kill -0 {pid} 2>/dev/null && test -s {quoted_remote} && echo READY", timeout=5)
        if ready.strip() != "READY":
            log_tail = runtime.exec(device, f"tail -n 20 /tmp/nika-capture-{capture_id}.log 2>/dev/null || true", timeout=5)
            raise RuntimeError(f"Capture process failed before startup: {log_tail.strip()}")
'''
if source.count(anchor) != 1:
    raise ValueError('unsupported capture launch structure')

@dataclasses.dataclass
class Limits:
    max_duration_sec: int = 2
    max_packets: int = 1
    max_bytes: int | None = None

class Runtime:
    def exec(self, device, command, timeout):
        result = subprocess.run(['sh', '-c', command], capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr

conditions = []
for label, body in [('recorded', source), ('readiness-fixed', source.replace(anchor, patch + anchor))]:
    tree = ast.parse(body)
    selected = {'RunningCapture', '_utc_now', '_shell_quote', 'detect_capture_backend', 'remote_paths', 'start_capture'}
    tree.body = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in selected]
    module = types.ModuleType('owned_capture_' + uuid.uuid4().hex)
    sys.modules[module.__name__] = module
    module.__dict__.update(dataclass=dataclasses.dataclass, datetime=datetime.datetime,
        timezone=datetime.timezone, re=re, shlex=shlex, LabRuntime=object,
        CaptureBackendKind=str, CaptureLimits=object)
    exec(compile(tree, 'pinned_capture.py', 'exec', flags=__future__.annotations.compiler_flag), module.__dict__)
    rows = []
    for role, interface in [('valid', 'lo'), ('observed', plan['interface'])]:
        cid = 'audit-probe-' + uuid.uuid4().hex
        pid = None
        try:
            result = module.start_capture(Runtime(), capture_id=cid, device='owned-container', interface=interface, capture_filter=None, limits=Limits())
            time.sleep(0.3)
            pid = int(Path(result.pid_path).read_text().strip())
            stat = Path('/proc') / str(pid) / 'stat'
            pcap = Path(result.remote_path)
            log = Path('/tmp') / ('nika-capture-' + cid + '.log')
            outcome = {'returned': dataclasses.asdict(result),
                'child_state': stat.read_text().split()[2] if stat.exists() else 'absent',
                'pcap_exists': pcap.exists(), 'pcap_bytes': pcap.stat().st_size if pcap.exists() else 0,
                'pcap_magic': pcap.read_bytes()[:4].hex() if pcap.exists() else '',
                'backend_log': log.read_text() if log.exists() else ''}
        except Exception as exc:
            outcome = {'raised': repr(exc)}
        finally:
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for suffix in ['.pcapng', '.pid', '.log']:
                (Path('/tmp') / ('nika-capture-' + cid + suffix)).unlink(missing_ok=True)
        rows.append({'role': role, 'interface': interface, 'outcome': outcome})
    conditions.append({'label': label, 'source_sha256': hashlib.sha256(body.encode()).hexdigest(), 'observations': rows})
version = subprocess.run(['dumpcap', '--version'], capture_output=True, text=True).stdout.splitlines()[:1]
print(json.dumps({'conditions': conditions, 'dumpcap_version': version, 'uid': os.getuid()}))
"""


def run_capture_probe(plan: CapturePlan, repo: Path, work: Path) -> dict:
    source_files(str(repo.resolve()), plan.commit)
    source = read_source(str(repo.resolve()), plan.commit, plan.source_path)
    if len(source.encode()) > 128_000:
        raise ValueError("capture source exceeds the bounded execution budget")
    image = subprocess.check_output(
        ["docker", "image", "inspect", plan.image, "--format", "{{.Id}}"],
        text=True,
        timeout=10,
    ).strip()
    request = {
        "plan": plan.model_dump(),
        "image_id": image,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "isolation": {
            "network": "none",
            "read_only": True,
            "uid": 0,
            "timeout_seconds": 30,
        },
    }
    key = hashlib.sha256(json.dumps([request, _WORKER]).encode()).hexdigest()
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{key}.json"
    if path.is_file():
        record = json.loads(path.read_text())
        if record.get("complete"):
            return {**record, "cached": True}
    name = "nika-audit-capture-" + uuid.uuid4().hex
    record = {"request": request, "container": name, "cached": False}
    with tempfile.TemporaryDirectory(prefix="nika-audit-capture-") as directory:
        folder = Path(directory)
        folder.joinpath("source.py").write_text(source)
        folder.joinpath("worker.py").write_text(_WORKER)
        folder.joinpath("plan.json").write_text(plan.model_dump_json())
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--network",
            "none",
            "--user",
            "0",
            "--read-only",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_RAW",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "KILL",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "--mount",
            f"type=bind,src={folder / 'source.py'},dst=/probe/source.py,readonly",
            "--mount",
            f"type=bind,src={folder / 'worker.py'},dst=/probe/worker.py,readonly",
            "--mount",
            f"type=bind,src={folder / 'plan.json'},dst=/probe/plan.json,readonly",
            "--entrypoint",
            "/usr/bin/python3",
            plan.image,
            "/probe/worker.py",
        ]
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                completed = subprocess.run(
                    command, stdout=stdout, stderr=stderr, timeout=30
                )
                record["returncode"] = completed.returncode
            except (OSError, subprocess.SubprocessError) as exc:
                record["error"] = str(exc)
            finally:
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", name], capture_output=True, timeout=10
                    )
                    remaining = subprocess.check_output(
                        ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
                        text=True,
                        timeout=10,
                    )
                    record["resources_removed"] = not remaining.strip()
                except (OSError, subprocess.SubprocessError) as exc:
                    record.update(resources_removed=False, cleanup_error=str(exc))
            for label, handle in (("stdout", stdout), ("stderr", stderr)):
                size = handle.tell()
                handle.seek(0)
                record[label] = handle.read(64_000).decode(errors="replace")
                record[label + "_truncated"] = size > 64_000
    if record.get("returncode") == 0 and not record.get("stdout_truncated"):
        try:
            record["measurement"] = json.loads(record["stdout"])
        except ValueError:
            record["error"] = "invalid capture probe receipt"
    elif not record.get("error"):
        record["error"] = f"capture probe exited with status {record.get('returncode')}"
    record["complete"] = bool(
        record.get("measurement")
        and record.get("resources_removed")
        and not record.get("error")
        and not record.get("stderr_truncated")
    )
    path.write_text(json.dumps(record, indent=1) + "\n")
    with (work / "attempts.jsonl").open("a") as journal:
        journal.write(json.dumps(record) + "\n")
    return record


def confirms_start_failure(receipt: dict, version: str) -> bool:
    if not receipt.get("complete") or not receipt.get("resources_removed"):
        return False
    measurement = receipt["measurement"]
    if measurement.get("dumpcap_version") != [version]:
        return False
    conditions = measurement.get("conditions") or []
    if [c.get("label") for c in conditions] != ["recorded", "readiness-fixed"]:
        return False
    if conditions[0].get("source_sha256") != receipt["request"]["source_sha256"]:
        return False
    if conditions[1].get("source_sha256") == conditions[0]["source_sha256"]:
        return False
    before, after = [c["observations"] for c in conditions]
    if any(
        [r["role"] for r in rows] != ["valid", "observed"] for rows in (before, after)
    ):
        return False
    if any(
        rows[0]["interface"] != "lo"
        or rows[1]["interface"] != receipt["request"]["plan"]["interface"]
        for rows in (before, after)
    ):
        return False

    def live(row):
        out = row["outcome"]
        return (
            out.get("returned", {}).get("capture_backend") == "dumpcap"
            and out.get("child_state") in {"S", "R", "D"}
            and out.get("pcap_magic") == "0a0d0d0a"
        )

    bad = before[1]["outcome"]
    return bool(
        live(before[0])
        and live(after[0])
        and bad.get("returned", {}).get("capture_backend") == "dumpcap"
        and bad.get("child_state") in {"Z", "absent"}
        and bad.get("pcap_exists") is False
        and "no such device" in bad.get("backend_log", "").lower()
        and after[1]["outcome"].get("raised", "").startswith("RuntimeError(")
    )
