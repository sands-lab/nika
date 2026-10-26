"""Read and write switch P4Runtime state for failure injection and checks.

Injectors talk to the switches from ``fabric_mgr`` with the controller's own
P4Runtime client code, through a one-off script rather than a manager
subcommand, so the controller's tooling exposes no fault operations. The
script is unlinked before it runs, so neither the filesystem nor the process
list shows which switch or table a check touches.

Unless ``logged`` is set, writes stay out of the controller's write log: they
model a change made outside the controller.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from nika.runtime.base import LabRuntime

_SCRIPT = r"""
import json, sys
sys.path.insert(0, MANAGER_DIR)
import p4rt_manager as m
req = json.loads(REQUEST)
switch = m.load_json(req["intent"])["switches"][req["switch"]]
atomicity = req.get("atomicity") or "CONTINUE_ON_ERROR"
client = m.connect(switch)
out = {"ok": True, "statuses": []}
try:
    index = m.P4InfoIndex(m.load_p4info(req["p4info"]))
    if req.get("pipeline"):
        info = m.load_p4info(req["pipeline"]["p4info"])
        with open(req["pipeline"]["json"], "rb") as handle:
            config = handle.read()
        try:
            client.set_pipeline(info, config)
            out["set_error"] = None
        except m.grpc.RpcError as exc:
            out["set_error"] = str(exc)
        out["pipeline"] = client.get_pipeline()
    updates = [m.build_update(index, spec) for spec in req.get("updates") or []]
    batches = [[u] for u in updates] if req.get("sequential") else [updates]
    for batch in batches:
        if not batch:
            continue
        if req.get("logged"):
            statuses = m.controller_write(client, index, req["switch"], batch, atomicity)
        else:
            statuses = m.write_updates(client, batch, atomicity)
        out["statuses"].extend(statuses)
    if req.get("read"):
        out["observed"] = m.observed_switch(client, index)
    if req.get("read_tables"):
        out["tables"] = {t: m.table_rows(client, index, t) for t in req["read_tables"]}
finally:
    client.close()
out["ok"] = all(s["code"] == "OK" for s in out["statuses"])
print(json.dumps(out, default=str))
"""


def p4runtime_write(
    runtime: LabRuntime,
    switch: str,
    updates: list[dict[str, Any]],
    *,
    sequential: bool = False,
    logged: bool = False,
    read: bool = False,
    read_tables: list[str] | None = None,
    atomicity: str = "CONTINUE_ON_ERROR",
    pipeline: dict[str, str] | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Send *updates* (``p4rt_manager.build_update`` specs) to *switch*.

    Returns ``{"ok", "statuses", ...}`` with one ``{"code", "message"}`` per
    update; ``observed`` holds the switch read when *read* is set and
    ``tables`` the rows of *read_tables*.
    """
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
        FABRIC_DIR,
        INTENT_PATH,
        MANAGER_DIR,
        P4INFO_PATH,
        _copy_in,
        parse_json_output,
    )

    request = {
        "intent": INTENT_PATH,
        "p4info": P4INFO_PATH,
        "switch": switch,
        "updates": updates,
        "sequential": sequential,
        "logged": logged,
        "read": read,
        "read_tables": read_tables or [],
        "atomicity": atomicity,
        "pipeline": pipeline,
    }
    script = _SCRIPT.replace("MANAGER_DIR", repr(MANAGER_DIR)).replace(
        "REQUEST", repr(json.dumps(request))
    )
    path = f"{FABRIC_DIR}/.{uuid.uuid4().hex[:12]}"
    _copy_in(runtime, "fabric_mgr", path, script.encode())
    raw = runtime.exec(
        "fabric_mgr",
        f"exec 3<{path}; rm -f {path}; python3 -B /dev/fd/3",
        timeout=timeout,
    )
    payload = parse_json_output(raw)
    if payload is None:
        raise RuntimeError(f"P4Runtime request to {switch} failed: {raw[-800:]}")
    return payload


def require_ok(result: dict[str, Any], what: str) -> dict[str, Any]:
    """Raise unless every update of a :func:`p4runtime_write` succeeded."""
    if not result.get("ok"):
        raise RuntimeError(f"{what} failed: {result.get('statuses')}")
    return result
