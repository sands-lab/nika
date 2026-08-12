"""Mock LLM agent that simulates BasicReActAgent behaviour without a real LLM.

The agent mirrors the two-phase architecture of BasicReActAgent:
  1. diagnosis phase  – calls lab MCP tools and emits a deterministic report
  2. submission phase – calls list_avail_problems + submit via task MCP server

Uses the session's ground truth and live lab device names (not hardcoded
``pc1``/``pc2``), so it works across release topologies.

Test-only. See ``docs/testing.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config
from agent.utils.mcp_servers import select_diagnosis_servers
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.runtime.factory import resolve_backend
from nika.utils.session import Session
from nika.utils.session_store import SessionStore

_ROUTER_HINTS = ("router", "leaf", "spine", "super_spine")


def _tool_text_list(result: object) -> list[str]:
    """Normalize MCP tool output into plain strings."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return [result]
    if not isinstance(result, list):
        return [str(result)]

    texts: list[str] = []
    for item in result:
        if isinstance(item, dict) and "text" in item:
            texts.append(str(item["text"]))
        else:
            texts.append(str(item))
    return texts


def _load_ground_truth(session_dir: str | None) -> dict[str, Any]:
    if not session_dir:
        return {}
    path = Path(session_dir) / "ground_truth.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _session_device_names(session_id: str) -> list[str]:
    """Best-effort list of running lab device names for this session."""
    try:
        from nika.workflows.session.containers import list_session_containers

        _sid, _lab, rows = list_session_containers(session_id)
    except Exception:  # noqa: BLE001 - mock falls back to ground truth only
        return []
    names: list[str] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _pick_pair(devices: list[str], preferred: list[str]) -> tuple[str, str] | None:
    pool = [d for d in preferred if d in devices] or list(devices)
    if len(pool) >= 2:
        return pool[0], pool[1]
    if len(pool) == 1:
        return pool[0], pool[0]
    return None


def _pick_router(devices: list[str]) -> str | None:
    for name in devices:
        lower = name.lower()
        if any(hint in lower for hint in _ROUTER_HINTS):
            return name
    return None


def _mock_diagnosis_tool_calls(
    *,
    backend: str,
    server_names: list[str],
    devices: list[str],
    preferred: list[str],
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = [("get_reachability", {})]
    if "pingmesh_mcp_server" in server_names:
        calls.append(("run_pingmesh_snapshot", {}))

    pair = _pick_pair(devices, preferred)
    if pair is not None:
        host_a, host_b = pair
        calls.append(("ping_pair", {"host_a": host_a, "host_b": host_b}))
        calls.append(("exec_shell", {"host_name": host_a, "command": "hostname"}))

    if backend == "containerlab":
        router = _pick_router(devices) or (preferred[0] if preferred else None)
        if router and "containerlab_srl_mcp_server" in server_names:
            calls.append(("srl_show_ip_route", {"device_name": router}))
    else:
        router = _pick_router(devices) or (preferred[0] if preferred else None)
        if router and "kathara_frr_mcp_server" in server_names:
            calls.append(("frr_show_ip_route", {"router_name": router}))
    return calls


def _mock_diagnosis_report(*, devices: list[str], preferred: list[str]) -> str:
    focus = ", ".join(preferred[:3]) if preferred else ", ".join(devices[:3]) or "lab"
    return (
        f"Anomaly detected involving device(s): {focus}. "
        "Mock diagnosis complete; submitting ground-truth root cause."
    )


class MockAgent:
    """Deterministic mock agent that mirrors the BasicReActAgent interface."""

    def __init__(
        self,
        session_id: str,
        model: str = "mock-v1",
        max_steps: int = 20,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.session = Session()
        self.session.load_running_session(session_id=session_id)

    def load_session(self) -> None:
        self.session.load_running_session(session_id=self.session_id)

    async def run(self, task_description: str) -> dict[str, Any]:
        self.load_session()
        diagnosis_report = await self._run_diagnosis(task_description)
        await self._run_submission(diagnosis_report)
        return {"diagnosis_report": diagnosis_report}

    async def _run_diagnosis(self, task_description: str) -> str:
        logger = self._make_logger(DIAGNOSIS)
        logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": task_description},
                "model": {"name": self.model},
            },
        )

        session_row = SessionStore().get_session(self.session_id)
        backend = resolve_backend(session_row)
        scenario = str(session_row.get("scenario_name") or "")
        server_names = select_diagnosis_servers(scenario, backend=backend)
        gt = _load_ground_truth(getattr(self.session, "session_dir", None))
        preferred = [str(d) for d in (gt.get("faulty_devices") or []) if d]
        devices = _session_device_names(self.session_id) or list(preferred)

        config = load_session_mcp_config(self.session_id, scenario, backend=backend)
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        tool_calls = _mock_diagnosis_tool_calls(
            backend=backend,
            server_names=server_names,
            devices=devices,
            preferred=preferred,
        )
        diagnosis_report = _mock_diagnosis_report(devices=devices, preferred=preferred)

        for tool_name, tool_input in tool_calls:
            if tool_name not in tools:
                continue
            logger.log(
                "tool_start",
                {"tool": {"name": tool_name}, "input": json.dumps(tool_input)},
            )
            try:
                tool_output = await tools[tool_name].ainvoke(tool_input)
            except Exception as exc:  # noqa: BLE001 - keep mock pipeline moving
                tool_output = f"tool_error: {exc}"
            logger.log(
                "tool_end",
                {
                    "output": str(tool_output),
                    "output_type": type(tool_output).__name__,
                },
            )

        logger.log("llm_end", {"text": diagnosis_report})
        return diagnosis_report

    async def _run_submission(self, diagnosis_report: str) -> None:
        logger = self._make_logger(SUBMISSION)

        session_row = SessionStore().get_session(self.session_id)
        backend = resolve_backend(session_row)
        scenario = str(session_row.get("scenario_name") or "")
        gt = _load_ground_truth(getattr(self.session, "session_dir", None))
        faulty_devices = [str(d) for d in (gt.get("faulty_devices") or []) if d]
        if not faulty_devices:
            devices = _session_device_names(self.session_id)
            faulty_devices = devices[:1] or (
                ["leaf1"] if backend == "containerlab" else ["pc1"]
            )

        logger.log(
            "llm_start",
            {
                "messages": {
                    "role": "user",
                    "content": (
                        f"Based on diagnosis: {diagnosis_report}. "
                        "Please call list_avail_problems and then submit."
                    ),
                },
                "model": {"name": self.model},
            },
        )

        begin_submission_mcp_phase(self.session_id)
        config = load_session_mcp_config(self.session_id, scenario, backend=backend)
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        logger.log(
            "tool_start", {"tool": {"name": "list_avail_problems"}, "input": "{}"}
        )
        avail_raw = await tools["list_avail_problems"].ainvoke({})
        avail = _tool_text_list(avail_raw)
        gt_names = gt.get("root_cause_name") or []
        if isinstance(gt_names, str):
            gt_names = [gt_names]
        session_root_cause = getattr(self.session, "root_cause_name", None)
        candidates = [str(n) for n in gt_names if n] + (
            [session_root_cause] if session_root_cause else []
        )
        mock_root_cause = next((c for c in candidates if c in avail), None)
        if mock_root_cause is None:
            mock_root_cause = avail[0] if avail else "link_down"
        logger.log(
            "tool_end",
            {"output": json.dumps(avail[:5]) + " ...", "output_type": "list"},
        )

        submission: dict[str, Any] = {
            "is_anomaly": True,
            "faulty_devices": faulty_devices,
            "root_cause_name": [mock_root_cause],
        }
        logger.log(
            "tool_start",
            {"tool": {"name": "submit"}, "input": json.dumps(submission)},
        )
        submit_result = await tools["submit"].ainvoke(submission)
        logger.log(
            "tool_end",
            {"output": str(submit_result), "output_type": type(submit_result).__name__},
        )

        logger.log(
            "llm_end",
            {
                "text": (
                    f"Submitted: root cause = {mock_root_cause}, "
                    f"faulty devices = {faulty_devices}"
                )
            },
        )

    def _make_logger(self, agent_name: str):
        """Return a MessageLogger for *agent_name*."""
        from agent.utils.loggers import MessageLogger  # noqa: PLC0415

        return MessageLogger(agent=agent_name, session_dir=self.session.session_dir)
