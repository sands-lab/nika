"""Compile the P4 pipeline once and program every BMv2 switch over P4Runtime."""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path
from typing import Any

from nika.net_env.p4_dc_fabric.fabric_manager.intent import (
    build_forwarding_intent,
)
from nika.net_env.p4_dc_fabric.topology_model import ClosFabricModel
from nika.runtime.base import LabRuntime
from nika.utils.logger import system_logger

logger = system_logger

FABRIC_DIR = "/tmp/p4_fabric"
MANAGER = "/opt/nika/p4rt_manager.py"
INTENT_PATH = f"{FABRIC_DIR}/intent.json"
P4INFO_PATH = f"{FABRIC_DIR}/fabric.p4info.txt"
JSON_PATH = f"{FABRIC_DIR}/fabric.json"
LAST_PATH = f"{FABRIC_DIR}/last.json"


def _exec(runtime: LabRuntime, host: str, cmd: str, timeout: float = 60.0) -> str:
    return runtime.exec(host, cmd, timeout=timeout)


def _copy_out(runtime: LabRuntime, host: str, src_path: str) -> bytes:
    container = runtime.get_container(host)
    stream, _stat = container.get_archive(src_path)
    buf = io.BytesIO()
    for chunk in stream:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r") as tar:
        member = tar.next()
        if member is None:
            raise RuntimeError(f"empty archive for {host}:{src_path}")
        handle = tar.extractfile(member)
        if handle is None:
            raise RuntimeError(f"missing file {host}:{src_path}")
        return handle.read()


def _copy_in(runtime: LabRuntime, host: str, dest_path: str, content: bytes) -> None:
    container = runtime.get_container(host)
    parent = str(Path(dest_path).parent)
    name = Path(dest_path).name
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    container.put_archive(parent, buf.getvalue())


def run_manager(
    runtime: LabRuntime,
    *args: str,
    timeout: float = 90.0,
    p4info: str = P4INFO_PATH,
    json_path: str = JSON_PATH,
) -> dict[str, Any]:
    cmd = (
        f"python3 {MANAGER} --intent {INTENT_PATH} --p4info {p4info} "
        f"--json {json_path} {' '.join(args)} > {LAST_PATH} 2> {FABRIC_DIR}/last.err; "
        f"echo $?"
    )
    status = _exec(runtime, "fabric_mgr", cmd, timeout=timeout).strip()
    raw = _copy_out(runtime, "fabric_mgr", LAST_PATH).decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        err = _copy_out(runtime, "fabric_mgr", f"{FABRIC_DIR}/last.err").decode(
            "utf-8", errors="replace"
        )[-800:]
        raise RuntimeError(
            f"fabric manager returned non-JSON (status={status}): {err or raw[:800]}"
        ) from exc
    return payload


def load_intent(runtime: LabRuntime) -> dict[str, Any]:
    return json.loads(_copy_out(runtime, "fabric_mgr", INTENT_PATH).decode())


_COMPILE_OK = "__NIKA_COMPILE_OK"


def compile_pipeline_cmd(program: str, p4info_name: str, json_name: str) -> str:
    """Build the remote p4c command. Skip same-path ``cp`` (GNU cp exits 1)."""
    base = Path(program).name
    dest = f"/tmp/{base}"
    stage = "true" if program == dest else f"cp {program} {dest}"
    inner = (
        f"{stage} && cd /tmp && "
        f"(p4c --std p4-16 --p4runtime-files {p4info_name} {base} "
        f"|| p4c-bm2-ss --std p4-16 --p4runtime-files {p4info_name} "
        f"-o {json_name} {base}) && test -f {json_name} && test -f {p4info_name} "
        f"&& echo {_COMPILE_OK}"
    )
    return f"({inner}) 2>&1"


def compile_pipeline_on_switch(
    runtime: LabRuntime,
    switch: str,
    program: str,
    p4info_name: str,
    json_name: str,
) -> None:
    compile_cmd = compile_pipeline_cmd(program, p4info_name, json_name)
    output = _exec(runtime, switch, compile_cmd, timeout=180)
    if _COMPILE_OK not in output:
        raise RuntimeError(f"p4c failed on {switch} for {program}: {output[-800:]}")


def _wait_for_grpc(
    runtime: LabRuntime, model: ClosFabricModel, timeout_sec: float
) -> None:
    deadline = time.time() + timeout_sec
    pending = list(model.fabric_switches())
    while pending and time.time() < deadline:
        still = [
            name
            for name in pending
            if not _exec(
                runtime, name, "pgrep -x simple_switch_grpc", timeout=10
            ).strip()
        ]
        pending = still
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeError(f"simple_switch_grpc not ready on {pending}")


def reconcile_fabric(
    runtime: LabRuntime,
    model: ClosFabricModel,
    *,
    wait_grpc_sec: float = 180.0,
) -> dict[str, Any]:
    """Compile once, push pipeline + forwarding intent, confirm Read matches."""
    _wait_for_grpc(runtime, model, wait_grpc_sec)
    _exec(runtime, "fabric_mgr", f"mkdir -p {FABRIC_DIR}", timeout=10)

    compiler = model.leaves[0]
    compile_pipeline_on_switch(
        runtime, compiler, "fabric.p4", "fabric.p4info.txt", "fabric.json"
    )
    for src_name, dest in (
        ("/tmp/fabric.json", JSON_PATH),
        ("/tmp/fabric.p4info.txt", P4INFO_PATH),
    ):
        _copy_in(runtime, "fabric_mgr", dest, _copy_out(runtime, compiler, src_name))

    intent = build_forwarding_intent(model)
    _copy_in(
        runtime,
        "fabric_mgr",
        INTENT_PATH,
        json.dumps(intent, indent=2).encode(),
    )

    result = run_manager(runtime, "apply", timeout=180)
    if not result.get("ok"):
        raise RuntimeError(f"P4Runtime apply failed: {result}")

    observed = run_manager(runtime, "read", timeout=120)
    _copy_in(
        runtime,
        "fabric_mgr",
        f"{FABRIC_DIR}/observed.json",
        json.dumps(observed, indent=2, default=str).encode(),
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        f"{FABRIC_DIR}/endpoint_state.json",
        json.dumps({"endpoints": intent["endpoints"]}, indent=2).encode(),
    )
    logger.info(
        "Programmed P4 fabric %s: %s switches",
        model.topo_size,
        len(intent["switches"]),
    )
    return {"intent": intent, "apply": result, "observed": observed}
