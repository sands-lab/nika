"""Evidence recorder: a deterministic agent that records a task for offline analysis.

The recorder runs in place of a diagnosis agent on a scheduled task whose
failure fires after a healthy baseline window. It runs a fixed probe set
through both windows, snapshots every node with the view catalog before the
injection and at the end of the incident window, exports the collected
telemetry, and abstains from submitting. It never reads ground truth while
recording, so every task of an environment yields the same files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.base import DiagnosisAgent
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MessageLogger
from nika.agents.recorder import probes as probe_mod
from nika.agents.recorder import telemetry_export as tx
from nika.agents.recorder.views import (
    DETECT_COMMAND,
    RECORDER_MARK,
    NodeFacts,
    views_for,
)
from nika.runtime.factory import resolve_backend
from nika.runtime.spec import MachineInventory
from nika.telemetry.management import TELEMETRY_COLLECTOR, TELEMETRY_SWITCH
from nika.utils.session_artifacts import write_json_atomic

RECORDING_DIR = "recording"
# Default baseline when the task has no timed event (healthy controls).
BASELINE_SEC = 120
INCIDENT_SEC = 240
PROBE_PERIOD_SEC = 20
IPERF_PERIOD_SEC = 60
# The baseline snapshot is taken this long before the planned injection.
BASELINE_SNAPSHOT_LEAD_SEC = 30
INJECTION_WAIT_SEC = 300
ALERT_JITTER_SEC = (10, 40)
SNAPSHOT_WORKERS = 16
ONOS_PATHS = (
    "/onos/v1/devices",
    "/onos/v1/devices/ports",
    "/onos/v1/links",
    "/onos/v1/hosts",
    "/onos/v1/flows",
    "/onos/v1/groups",
    "/onos/v1/topology",
    "/onos/v1/configuration/org.onosproject.openflow.controller.impl.OpenFlowControllerImpl",
)


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).isoformat(timespec="seconds")


def _is_monitoring_node(name: str) -> bool:
    return name in (TELEMETRY_COLLECTOR, TELEMETRY_SWITCH) or name.startswith(
        "telemetry_flow_"
    )


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


class RecorderAgent(DiagnosisAgent):
    """Records baseline and incident evidence; submits nothing."""

    def __init__(
        self,
        session_id: str,
        model: str = "recorder",
        max_steps: int = 1,
        stream_output: bool = True,
    ) -> None:
        from nika.utils.session import Session

        self.session_id = session_id
        self.model = model
        self.llm_provider = None
        self.max_steps = max_steps
        self.reasoning_effort = None
        self.max_tokens = None
        self.stream_output = stream_output
        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir = self.trace_dir = session.session_dir
        self.bundle = Path(self.session_dir) / RECORDING_DIR
        self.errors: list[str] = []
        self._report: str | None = None

    # -- setup ----------------------------------------------------------------

    def _api_and_inventory(self) -> tuple[Any, MachineInventory | None, dict]:
        from nika.service.lab.host_api import create_host_api
        from nika.utils.session_store import SessionStore

        meta = SessionStore().get_session(self.session_id)
        lab_name = meta.get("lab_name") or (meta.get("scenario_params") or {}).get(
            "lab_name"
        )
        api = create_host_api(
            lab_name=lab_name, backend=resolve_backend(meta), session_meta=meta
        )
        identities = (meta.get("metadata") or {}).get("machine_identities")
        inventory = MachineInventory.from_dict(identities) if identities else None
        return api, inventory, meta

    def _planned_injection_offset(self) -> float:
        path = Path(self.session_dir) / "schedule.json"
        try:
            plan = json.loads(path.read_text())
        except (OSError, ValueError):
            return BASELINE_SEC
        offsets = []
        for event in plan.get("events") or []:
            if event.get("problem"):
                at = event.get("at", 0)
                offsets.append(float(at) if not isinstance(at, str) else _seconds(at))
        return min(offsets) if offsets else BASELINE_SEC

    def _injection_finished(self) -> float | None:
        path = Path(self.session_dir) / "schedule-events.jsonl"
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("status") == "finished" and rec.get("operation") == "start":
                return datetime.fromisoformat(rec["timestamp"]).timestamp()
        return None

    def _detect(self, api: Any, inventory: MachineInventory | None) -> list[NodeFacts]:
        identities = inventory.to_dict() if inventory else {}
        names = sorted(n for n in identities if not _is_monitoring_node(n))

        def one(name: str) -> NodeFacts:
            ident = identities.get(name) or {}
            out = api.exec_cmd(name, DETECT_COMMAND, timeout=15)
            return NodeFacts(
                name=name,
                role=str(ident.get("role", "")),
                capabilities=frozenset(ident.get("capabilities") or ()),
                tools=frozenset(t.strip() for t in out.split() if t.strip()),
            )

        with ThreadPoolExecutor(SNAPSHOT_WORKERS) as pool:
            return list(pool.map(one, names))

    # -- snapshots ------------------------------------------------------------

    def _snapshot(self, api: Any, facts: list[NodeFacts], root: Path) -> dict:
        jobs = [(n, v) for n in facts for v in views_for(n)]

        def one(job):
            node, view = job
            start = time.time()
            try:
                out = api.exec_cmd(
                    node.name, f"{RECORDER_MARK} {view.command}", timeout=view.timeout
                )
            except Exception as exc:  # noqa: BLE001 - a failed view is recorded
                out = f"recorder_error: {exc}"
            path = root / "devices" / node.name / f"{view.name}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not out.strip():
                out = "(no output)"
            path.write_text(out if out.endswith("\n") else out + "\n")
            return node.name, view.name, round(time.time() - start, 2), len(out)

        started = time.time()
        # Process listings run alone first, so they do not show the other views.
        first = [j for j in jobs if j[1].name == "processes"]
        rest = [j for j in jobs if j[1].name != "processes"]
        with ThreadPoolExecutor(SNAPSHOT_WORKERS) as pool:
            results = list(pool.map(one, first)) + list(pool.map(one, rest))
        self._controller_views(api, facts, root)
        return {
            "taken_at": _now_iso(started),
            "duration_sec": round(time.time() - started, 1),
            "files": len(results),
            "slowest": sorted(results, key=lambda r: -r[2])[:5],
        }

    def _controller_views(self, api: Any, facts: list[NodeFacts], root: Path) -> None:
        """Controller-plane reads that go through a controller, not a node shell."""
        out = root / "controllers"
        caps = {c for n in facts for c in n.capabilities}
        if "p4runtime" in caps:
            from nika.service.kathara.bmv2_api import BMv2APIMixin

            out.mkdir(parents=True, exist_ok=True)
            for args, name in (
                ("read", "p4runtime_read"),
                ("counters", "p4runtime_counters"),
            ):
                try:
                    text = BMv2APIMixin.p4rt_exec(api, args, timeout=120)
                except Exception as exc:  # noqa: BLE001
                    text = f"recorder_error: {exc}"
                (out / f"{name}.json").write_text(text + "\n")
        if "onos" in caps:
            from nika.service.kathara.sdn_api import SdnAPIMixin

            out.mkdir(parents=True, exist_ok=True)
            bodies = {}
            for path in ONOS_PATHS:
                try:
                    bodies[path] = SdnAPIMixin.sdn_onos_rest(api, path)["body"]
                except Exception as exc:  # noqa: BLE001
                    bodies[path] = {"error": str(exc)[:500]}
            (out / "onos_rest.json").write_text(json.dumps(bodies, indent=1) + "\n")
            try:
                log = SdnAPIMixin.sdn_controller_logs(api, rows=300)["log"]
            except Exception as exc:  # noqa: BLE001
                log = f"recorder_error: {exc}"
            (out / "onos_karaf.log").write_text(log + "\n")

    # -- probes ---------------------------------------------------------------

    def _probe_loop(
        self, prober: probe_mod.Prober, stop: threading.Event, t0: float
    ) -> None:
        ping = _JsonlWriter(self.bundle / "probes" / "pingmesh.jsonl")
        app = _JsonlWriter(self.bundle / "probes" / "http_dns.jsonl")
        bw = _JsonlWriter(self.bundle / "probes" / "iperf.jsonl")
        iperf_runs = 0
        next_iperf = t0
        while not stop.is_set():
            start = time.time()
            stamp = {"time": _now_iso(start)}
            try:
                snap = prober.pingmesh()
                ping.write({**stamp, **snap})
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"pingmesh: {exc}")
            try:
                for row in prober.http_dns_round():
                    app.write({**stamp, **row})
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"http_dns: {exc}")
            if time.time() >= next_iperf:
                row = prober.iperf(iperf_runs)
                iperf_runs += 1
                if row:
                    bw.write({"time": _now_iso(), **row})
                next_iperf = time.time() + IPERF_PERIOD_SEC
            stop.wait(max(0.0, PROBE_PERIOD_SEC - (time.time() - start)))

    # -- main -----------------------------------------------------------------

    def _alert_offset(self) -> float:
        lo, hi = ALERT_JITTER_SEC
        digest = hashlib.sha256(self.session_id.encode()).digest()
        return lo + (hi - lo) * digest[0] / 255

    def record(self, task_description: str) -> dict[str, Any]:
        t0 = time.time()
        if self.bundle.exists():
            shutil.rmtree(self.bundle)
        self.bundle.mkdir(parents=True)
        api, inventory, meta = self._api_and_inventory()
        planned = self._planned_injection_offset()
        facts = self._detect(api, inventory)
        plan = probe_mod.build_plan(api, inventory, meta.get("metadata"))
        prober = probe_mod.Prober(api, plan)
        write_json_atomic(
            self.bundle / "task.json",
            {
                "scenario": meta.get("scenario_name"),
                "description": _network_brief(task_description),
                "segments": _topology_segments(task_description),
                "devices": [
                    {
                        "name": n.name,
                        "role": n.role,
                        "capabilities": sorted(n.capabilities),
                    }
                    for n in facts
                ],
                "probe_plan": plan.to_dict(),
                "telemetry": tx.telemetry_kinds(meta),
            },
        )
        stop = threading.Event()
        prober_thread = threading.Thread(
            target=self._probe_loop, args=(prober, stop, t0), daemon=True
        )
        prober_thread.start()

        # Baseline snapshot shortly before the planned injection.
        time.sleep(max(0.0, t0 + planned - BASELINE_SNAPSHOT_LEAD_SEC - time.time()))
        baseline = self._snapshot(api, facts, self.bundle / "_before_fault")

        # Incident window starts when the injection finished (or at the
        # planned time on healthy controls).
        deadline = t0 + planned + INJECTION_WAIT_SEC
        injected = None
        while time.time() < deadline:
            injected = self._injection_finished()
            if (
                injected
                or time.time() >= t0 + planned
                and not self._has_problem_event()
            ):
                break
            time.sleep(2)
        incident_start = injected or t0 + planned
        time.sleep(max(0.0, incident_start + INCIDENT_SEC - time.time()))
        stop.set()
        prober_thread.join(timeout=120)
        incident = self._snapshot(api, facts, self.bundle)
        traces = prober.traceroutes()
        (self.bundle / "probes" / "traceroute.json").write_text(
            json.dumps(traces, indent=1) + "\n"
        )
        prober.close()
        end = time.time()

        telemetry = self._export_telemetry(api, meta, facts, t0, end)
        alert = incident_start + self._alert_offset()
        write_json_atomic(
            self.bundle / "timeline.json",
            {
                "recording_start": _now_iso(t0),
                "alert_time": _now_iso(alert),
                "state_snapshot_time": incident["taken_at"],
                "recording_end": _now_iso(end),
                "note": "Probes and telemetry cover recording_start..recording_end. The alert fired at alert_time; state files under devices/ and controllers/ were captured at state_snapshot_time.",
            },
        )
        self._write_answer(t0, planned, injected, alert, baseline, incident, telemetry)
        return {"baseline": baseline, "incident": incident, "telemetry": telemetry}

    def _has_problem_event(self) -> bool:
        try:
            plan = json.loads((Path(self.session_dir) / "schedule.json").read_text())
        except (OSError, ValueError):
            return False
        return any(e.get("problem") for e in plan.get("events") or [])

    def _export_telemetry(self, api, meta, facts, start, end) -> dict:
        from nika.telemetry import prometheus

        kinds = tx.telemetry_kinds(meta)
        out = self.bundle / "telemetry"
        stats: dict[str, Any] = {"kinds": kinds}
        steps = {
            "prometheus": lambda: tx.export_prometheus(
                api, out / "prometheus", start - 10, end, prometheus.scrape_interval()
            ),
            "syslog": lambda: tx.export_syslog(
                api, out / "syslog.log", start - 10, end
            ),
            "netflow": lambda: tx.export_netflow(
                api, out / "netflow.csv", start - 10, end
            ),
        }
        out.mkdir(parents=True, exist_ok=True)
        for kind in kinds:
            if kind in steps:
                try:
                    stats[kind] = steps[kind]()
                except Exception as exc:  # noqa: BLE001
                    stats[kind] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
        int_nodes = [
            n.name
            for n in facts
            if "telemetry" in n.capabilities and n.role == "infrastructure"
        ]
        if int_nodes:
            stats["int"] = tx.export_int(api, int_nodes, out / "int", start - 10, end)
        return stats

    def _write_answer(
        self, t0, planned, injected, alert, baseline, incident, telemetry
    ) -> None:
        """Recorder metadata and ground truth; not part of the model input."""
        answer = self.bundle / "_label"
        answer.mkdir(exist_ok=True)
        for name in ("ground_truth.json", "schedule.json", "schedule-events.jsonl"):
            src = Path(self.session_dir) / name
            if src.exists():
                shutil.copy(src, answer / name)
        write_json_atomic(
            answer / "recording.json",
            {
                "recording_start": _now_iso(t0),
                "planned_injection_offset_sec": planned,
                "injection_finished": _now_iso(injected) if injected else None,
                "alert_time": _now_iso(alert),
                "baseline_snapshot": baseline,
                "incident_snapshot": incident,
                "telemetry": telemetry,
                "errors": self.errors[:50],
            },
        )

    async def diagnose(self, task_description: str) -> str:
        # The scheduler re-runs diagnosis until its timeline completes; the
        # recording already covers that time.
        if self._report is not None:
            return self._report
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": "recording"},
                "model": {"name": self.model},
            },
        )
        try:
            summary = await asyncio.to_thread(self.record, task_description)
        except Exception:
            self.errors.append(traceback.format_exc())
            write_json_atomic(
                self.bundle / "_label" / "recording_error.json",
                {"errors": self.errors},
            )
            raise
        report = (
            "Recorded task data: "
            + json.dumps(
                {
                    k: v.get("files") if isinstance(v, dict) and "files" in v else v
                    for k, v in summary.items()
                },
                default=str,
            )[:2000]
        )
        logger.log("llm_end", {"text": report})
        self._report = report
        return report

    async def submit(self, diagnosis_report: str, context: dict[str, Any]) -> str:
        """Submit a fixed placeholder so the trial counts as finished.

        Recorder trials are not scored; without a submission the runner marks
        them failed and batch resume would record them again.
        """
        from langchain_mcp_adapters.client import MultiServerMCPClient

        from nika.agents.mcp_config import load_session_mcp_config

        logger = MessageLogger(phase=SUBMISSION, session_dir=self.trace_dir)
        config = load_session_mcp_config(
            self.session_id,
            str(getattr(self.session, "scenario_name", "") or ""),
            backend=resolve_backend(self.session),
            phase=SUBMISSION,
        )
        tools = {
            t.name: t
            for t in await MultiServerMCPClient(connections=config).get_tools()
        }
        placeholder = {"is_anomaly": False, "root_causes": []}
        result = await tools["submit"].ainvoke(placeholder)
        text = f"Placeholder submission (recorder): {result}"
        logger.log("llm_end", {"text": text})
        return text


def _seconds(value: str) -> float:
    import re

    total = 0.0
    for num, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)?", value):
        total += float(num) * {"ms": 0.001, "h": 3600, "m": 60, "s": 1, "": 1}[unit]
    return total


def _network_brief(task_description: str) -> str:
    """The network part of the live task prompt, without tool-use instructions."""
    text = task_description
    marker = "Your goal is"
    if marker in text:
        text = text.split(marker, 1)[0]
    prefix = (
        "You are provided with the following network description and its current state:"
    )
    return text.replace(prefix, "").strip()


_SEGMENT = re.compile(r"\(([^()]*)\)")


def _topology_segments(task_description: str) -> list[list[dict[str, str]]]:
    """Segments from the ``Topology:`` line: each lists its (device, port) ends.

    A segment with two ends is a point-to-point link; more ends share one LAN.
    """
    for line in task_description.splitlines():
        if line.startswith("Topology:"):
            segments = []
            for body in _SEGMENT.findall(line):
                ends = []
                for item in body.split(","):
                    device, _, port = item.strip().partition(":")
                    if device:
                        ends.append({"device": device, "port": port})
                if len(ends) >= 2:
                    segments.append(ends)
            return segments
    return []
