"""See sitecustomize.py. Wraps NIKA's lab startup path with timers."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

LOG = os.environ["NIKA_PHASE_LOG"]
_ctx = threading.local()
_lock = threading.Lock()


def _emit(record: dict) -> None:
    record.setdefault("call", getattr(_ctx, "call", None))
    record.setdefault("session", getattr(_ctx, "session", None))
    record.setdefault("scenario", getattr(_ctx, "scenario", None))
    record.setdefault("size", getattr(_ctx, "size", None))
    record["pid"] = os.getpid()
    with _lock, open(LOG, "a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _timed(name: str):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0, w0 = time.monotonic(), time.time()
            try:
                return fn(*args, **kwargs)
            finally:
                _emit(
                    {"phase": name, "start": w0, "sec": round(time.monotonic() - t0, 3)}
                )

        wrapper.__phase_probe__ = True
        return wrapper

    return deco


# ------------------------------------------------------------------ convergence read-out


def _docker(*args: str, timeout: float = 60) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    ).stdout


def _iso(s: str) -> float | None:
    """RFC 3339 UTC time (docker: nanoseconds, k8s: seconds) -> epoch seconds."""
    import re

    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z$", s.strip())
    if not m:
        return None
    frac = float("0" + m.group(2)) if m.group(2) else 0.0
    return datetime.fromisoformat(m.group(1) + "+00:00").timestamp() + frac


def _containers(lab_hash: str) -> dict[str, str]:
    """machine name -> container id for one Kathara lab."""
    out = {}
    for line in _docker(
        "ps",
        "--filter",
        "label=app=kathara",
        "--filter",
        f"label=lab_hash={lab_hash}",
        "--format",
        '{{.ID}} {{.Label "name"}}',
    ).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[1]] = parts[0]
    return out


def _bgp(cid: str) -> dict | None:
    raw = _docker("exec", cid, "vtysh", "-c", "show bgp summary json", timeout=30)
    if not raw.strip().startswith("{"):
        return None
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError:
        return None
    peers = (summary.get("ipv4Unicast") or {}).get("peers") or {}
    if not peers:
        return None
    est = [
        p.get("peerUptimeEstablishedEpoch")
        for p in peers.values()
        if p.get("state") == "Established"
    ]
    routes_raw = _docker(
        "exec", cid, "vtysh", "-c", "show bgp ipv4 unicast detail json", timeout=60
    )
    last_update = None
    try:
        routes = json.loads(routes_raw).get("routes") or {}
        epochs = [
            p.get("lastUpdate", {}).get("epoch")
            for paths in routes.values()
            for p in paths
        ]
        epochs = [e for e in epochs if e]
        last_update = max(epochs) if epochs else None
    except (json.JSONDecodeError, AttributeError):
        pass
    return {
        "peers": len(peers),
        "established": len(est),
        "first_up": min(est) if est else None,
        "last_up": max(est) if est else None,
        "last_route_update": last_update,
    }


def _k8s(cid: str) -> dict:
    out = {}
    for kind in ("nodes", "pods"):
        raw = _docker(
            "exec", cid, "kubectl", "get", kind, "-A", "-o", "json", timeout=60
        )
        try:
            items = json.loads(raw).get("items") or []
        except json.JSONDecodeError:
            continue
        ready = []
        for it in items:
            for c in (it.get("status") or {}).get("conditions") or []:
                if (
                    c.get("type") == "Ready"
                    and c.get("status") == "True"
                    and c.get("lastTransitionTime")
                ):
                    ready.append(_iso(c["lastTransitionTime"]))
        ready = [r for r in ready if r]
        out[kind] = {
            "count": len(items),
            "ready": len(ready),
            "last_ready": max(ready) if ready else None,
        }
    return out


def _convergence(net_env) -> None:
    lab = getattr(net_env, "lab", None)
    if (
        lab is None
        or not getattr(lab, "machines", None)
        or not getattr(lab, "hash", None)
    ):
        return
    cids = _containers(lab.hash)
    names = [n for n in lab.machines if n in cids]
    if not names:
        return
    started = {}
    for chunk in range(0, len(names), 50):
        part = names[chunk : chunk + 50]
        for line in _docker(
            "inspect", "-f", "{{.Name}} {{.State.StartedAt}}", *[cids[n] for n in part]
        ).splitlines():
            cname, ts = line.split(" ", 1)
            started[cname.lstrip("/")] = _iso(ts)
    starts = [v for v in started.values() if v]
    with ThreadPoolExecutor(8) as pool:
        bgp = dict(zip(names, pool.map(lambda n: _bgp(cids[n]), names)))
    routers = {n: b for n, b in bgp.items() if b}
    rec = {
        "phase": "convergence_readout",
        "machines": len(names),
        "containers_first_start": min(starts) if starts else None,
        "containers_last_start": max(starts) if starts else None,
        "bgp_routers": len(routers),
        "bgp_sessions": sum(b["peers"] for b in routers.values()),
        "bgp_established": sum(b["established"] for b in routers.values()),
        "bgp_first_up": min(
            (b["first_up"] for b in routers.values() if b["first_up"]), default=None
        ),
        "bgp_last_up": max(
            (b["last_up"] for b in routers.values() if b["last_up"]), default=None
        ),
        "bgp_last_route_update": max(
            (
                b["last_route_update"]
                for b in routers.values()
                if b["last_route_update"]
            ),
            default=None,
        ),
    }
    k3s = [n for n in names if n == "controller"]
    if k3s:
        rec["k8s"] = _k8s(cids[k3s[0]])
    _emit(rec)


# ------------------------------------------------------------------ patches


def _patch() -> None:
    from Kathara.manager.Kathara import Kathara
    from nika.runtime.kathara.runtime import KatharaRuntime
    import nika.runtime.kathara.runtime as kruntime
    import nika.traffic.workload as workload
    import nika.workflows.env.start as start

    kruntime.record_container_taps = _timed("record_taps")(
        kruntime.record_container_taps
    )
    orig_workload = workload.start_workload

    @functools.wraps(orig_workload)
    def start_workload(runtime, wl):
        # The fixed traffic settle sleep at the end of start_workload.
        _emit(
            {
                "phase": "workload_settle_sleep",
                "sec": float(getattr(wl, "settle_sec", 0) or 0),
            }
        )
        return orig_workload(runtime, wl)

    workload.start_workload = start_workload

    Kathara.deploy_lab = _timed("kathara_deploy_lab")(Kathara.deploy_lab)
    Kathara.undeploy_lab = _timed("kathara_undeploy_lab")(Kathara.undeploy_lab)
    KatharaRuntime.deploy = _timed("runtime_deploy")(KatharaRuntime.deploy)
    KatharaRuntime.destroy = _timed("runtime_destroy")(KatharaRuntime.destroy)
    KatharaRuntime._wait_deploy_ready = _timed("wait_containers_running")(
        KatharaRuntime._wait_deploy_ready
    )
    start.verify_lab_with_retry = _timed("startup_verify")(start.verify_lab_with_retry)

    instance_methods = {
        "deploy": "env_deploy",
        "_ensure_runtime_files": "generate_configs",
        "_ensure_docker_images": "ensure_images",
        "preload_workload_images": "k8s_image_preload",
        "sync_client_hosts": "sync_client_hosts",
        "post_deploy": "post_deploy",
        "start_business_workload": "start_workload",
        "undeploy": "env_undeploy",
    }
    orig_get = start.get_net_env_instance

    def get_net_env_instance(*args, **kwargs):
        env = orig_get(*args, **kwargs)
        for attr, name in instance_methods.items():
            fn = getattr(env, attr, None)
            if callable(fn) and not getattr(fn, "__phase_probe__", False):
                setattr(env, attr, _timed(name)(fn))
        _ctx.env = env
        return env

    start.get_net_env_instance = get_net_env_instance
    orig_start = start.start_net_env

    @functools.wraps(orig_start)
    def start_net_env(scenario, topo_size=None, *args, **kwargs):
        import uuid

        _ctx.call = uuid.uuid4().hex[:8]
        _ctx.session, _ctx.scenario, _ctx.size = (
            kwargs.get("session_id"),
            scenario,
            topo_size,
        )
        _ctx.env = None
        t0, w0 = time.monotonic(), time.time()
        sid = None
        try:
            sid = orig_start(scenario, topo_size, *args, **kwargs)
            return sid
        finally:
            _ctx.session = sid or _ctx.session
            _emit(
                {
                    "phase": "start_net_env",
                    "start": w0,
                    "sec": round(time.monotonic() - t0, 3),
                    "ok": sid is not None,
                }
            )
            if sid is not None and _ctx.env is not None:
                try:
                    _convergence(_ctx.env)
                except Exception as exc:  # noqa: BLE001 - measurement only
                    _emit({"phase": "convergence_readout", "error": repr(exc)})

    start.start_net_env = start_net_env


_patch()
