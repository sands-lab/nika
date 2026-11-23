"""FastAPI app: JSON API over a dataset folder plus the built frontend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .dataset import Dataset, Task

DEFAULT_STATIC = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(
    data_dir: str | Path | None = None, static_dir: str | Path | None = None
) -> FastAPI:
    data = Dataset(data_dir or os.environ.get("EXPLORER_DATA", "/data"))
    static = Path(static_dir or os.environ.get("EXPLORER_STATIC", DEFAULT_STATIC))
    app = FastAPI(title="NIKA dataset explorer")
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.state.dataset = data

    def get_task(tid: str) -> Task:
        task = data.task(tid)
        if task is None:
            raise HTTPException(404, f"unknown task {tid}")
        return task

    def found(value: Any, what: str) -> Any:
        if value is None:
            raise HTTPException(404, f"{what} not found")
        return value

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "data_dir": str(data.root), "tasks": len(data.task_ids())}

    @app.get("/api/tasks")
    def tasks() -> list[dict[str, Any]]:
        return data.list_tasks()

    @app.get("/api/tasks/{tid}")
    def task_detail(tid: str) -> dict[str, Any]:
        return get_task(tid).detail()

    @app.get("/api/tasks/{tid}/label")
    def label(tid: str) -> dict[str, Any]:
        return found(get_task(tid).label(), "label")

    @app.get("/api/tasks/{tid}/devices/{device}/views/{view}")
    def view(
        tid: str, device: str, view: str, phase: str = "snapshot"
    ) -> dict[str, Any]:
        text = found(get_task(tid).view_text(device, view, phase), "view")
        return {"device": device, "view": view, "phase": phase, "text": text}

    @app.get("/api/tasks/{tid}/controllers/{name}")
    def controller(tid: str, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "text": found(get_task(tid).controller_text(name), "controller file"),
        }

    @app.get("/api/tasks/{tid}/topology.png")
    def topology_png(tid: str) -> FileResponse:
        path = get_task(tid).dir / "topology.png"
        return FileResponse(found(path if path.is_file() else None, "topology.png"))

    @app.get("/api/tasks/{tid}/metrics")
    def metrics(tid: str) -> list[dict[str, Any]]:
        return get_task(tid).metrics()

    @app.get("/api/tasks/{tid}/metrics/{metric}")
    def metric(
        tid: str,
        metric: str,
        device: list[str] | None = Query(None),
        rate: str = "auto",
    ) -> dict[str, Any]:
        return found(get_task(tid).metric_series(metric, device, rate), "metric")

    @app.get("/api/tasks/{tid}/interface")
    def interface(tid: str, device: str, port: str) -> dict[str, Any]:
        return get_task(tid).interface(device, port)

    @app.get("/api/tasks/{tid}/syslog")
    def syslog(
        tid: str,
        device: list[str] | None = Query(None),
        program: list[str] | None = Query(None),
        q: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        rows = get_task(tid).syslog_rows
        needle = q.lower() if q else None
        matched = [
            r
            for r in rows
            if (not device or r["device"] in device)
            and (not program or r["program"] in program)
            and (start is None or (r["t"] or 0) >= start)
            and (end is None or (r["t"] or 0) <= end)
            and (
                not needle
                or needle in r["message"].lower()
                or needle in r["program"].lower()
            )
        ]
        return {
            "total": len(rows),
            "matched": len(matched),
            "rows": matched[:limit],
            "devices": sorted({r["device"] for r in rows}),
            "programs": sorted({r["program"] for r in rows}),
        }

    @app.get("/api/tasks/{tid}/netflow")
    def netflow(
        tid: str,
        exporter: list[str] | None = Query(None),
        q: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        return get_task(tid).netflow(exporter, q, start, end, limit)

    @app.get("/api/tasks/{tid}/probes/pingmesh")
    def pingmesh(tid: str) -> dict[str, Any]:
        return get_task(tid).pingmesh()

    @app.get("/api/tasks/{tid}/probes/http_dns")
    def http_dns(tid: str) -> dict[str, Any]:
        return get_task(tid).http_dns()

    @app.get("/api/tasks/{tid}/probes/iperf")
    def iperf(tid: str) -> dict[str, Any]:
        return get_task(tid).iperf()

    @app.get("/api/tasks/{tid}/probes/traceroute")
    def traceroute(tid: str) -> list[dict[str, Any]]:
        return get_task(tid).traceroute()

    @app.get("/api/tasks/{tid}/int/summary")
    def int_summary(tid: str) -> dict[str, Any]:
        return get_task(tid).int_summary

    @app.get("/api/tasks/{tid}/int/records")
    def int_records(
        tid: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)
    ) -> dict[str, Any]:
        return get_task(tid).int_records(offset, limit)

    if static.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=static / "assets", check_dir=False),
            name="assets",
        )

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404)
            candidate = (static / path).resolve()
            if path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static / "index.html")

    return app


app = create_app()
