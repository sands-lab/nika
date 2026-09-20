"""Starlette app for the local session viewer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from nika.view.catalog import (
    RAW_ALLOWLIST,
    aggregate_benchmark_runs,
    build_session_facets,
    detail_session_dir,
    discover_sessions,
    filter_sessions,
    find_session_dir,
    list_selectable_roots,
    load_scores,
    read_raw_artifact,
    resolve_results_selection,
)
from nika.view.models import ResultsRootsResponse, SessionListResponse, TimelineResponse
from nika.view.timeline import build_session_timeline

_WWW_DIST = Path(__file__).resolve().parent / "www" / "dist"


def _error(
    message: str, *, status: int = 400, error_type: str = "Error"
) -> JSONResponse:
    return JSONResponse(
        {"error": message, "error_type": error_type},
        status_code=status,
    )


def create_view_app(*, results_root: Path) -> Starlette:
    """Build the read-only view API + static SPA."""

    base_root = Path(results_root).resolve()

    def _active_root(request: Request) -> Path | JSONResponse:
        try:
            return resolve_results_selection(
                base_root, request.query_params.get("root")
            )
        except ValueError as exc:
            return _error(str(exc), status=400)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "results_root": str(base_root),
                "roots": list_selectable_roots(base_root),
            }
        )

    async def roots(request: Request) -> JSONResponse:
        selected = request.query_params.get("root") or "."
        try:
            active = resolve_results_selection(base_root, selected)
        except ValueError as exc:
            return _error(str(exc), status=400)
        body = ResultsRootsResponse(
            base_root=str(base_root),
            selected_root=selected if selected not in {"", None} else ".",
            results_root=str(active),
            roots=list_selectable_roots(base_root),
        )
        return JSONResponse(body.model_dump())

    async def sessions(request: Request) -> JSONResponse:
        active = _active_root(request)
        if isinstance(active, JSONResponse):
            return active
        status = request.query_params.get("status", "all")
        if status not in {"running", "finished", "all"}:
            return _error("status must be running, finished, or all")
        has_score_raw = request.query_params.get("has_score")
        has_score: bool | None = None
        if has_score_raw in {"1", "true", "yes"}:
            has_score = True
        elif has_score_raw in {"0", "false", "no"}:
            has_score = False
        all_items = discover_sessions(results_root=active)
        facets = build_session_facets(all_items)
        trial_raw = request.query_params.get("trial_index")
        trial_index: int | None = None
        if trial_raw not in (None, ""):
            try:
                trial_index = int(trial_raw)
            except ValueError:
                return _error("trial_index must be an integer")
        items = filter_sessions(
            all_items,
            status=status,  # type: ignore[arg-type]
            scenario=request.query_params.get("scenario"),
            agent=request.query_params.get("agent"),
            model=request.query_params.get("model"),
            problem=request.query_params.get("problem"),
            failure_domain=request.query_params.get("failure_domain"),
            topo_size=request.query_params.get("topo_size"),
            trial_index=trial_index,
            q=request.query_params.get("q"),
            has_score=has_score,
        )
        selected = request.query_params.get("root") or "."
        body = SessionListResponse(
            sessions=items,
            benchmarks=aggregate_benchmark_runs(items),
            facets=facets,
            results_root=str(active),
            selected_root=selected,
            total=len(items),
        )
        return JSONResponse(body.model_dump())

    def _resolve(request: Request, session_id: str) -> Path | JSONResponse:
        active = _active_root(request)
        if isinstance(active, JSONResponse):
            return active
        session_dir = find_session_dir(session_id, results_root=active)
        if session_dir is None:
            return _error(
                f"Session not found: {session_id}", status=404, error_type="NotFound"
            )
        return session_dir

    async def session_detail(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        active = _active_root(request)
        assert not isinstance(active, JSONResponse)
        detail = detail_session_dir(resolved, results_root=active)
        if detail is None:
            return _error(
                f"Session not found: {session_id}", status=404, error_type="NotFound"
            )
        return JSONResponse(detail.model_dump())

    async def session_timeline(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        source = request.query_params.get("source")
        if source is not None and source not in {"agent", "nika", "merged"}:
            return _error("source must be agent, nika, or merged")
        filter_source = None if source in {None, "merged"} else source
        events = build_session_timeline(resolved, source=filter_source)
        body = TimelineResponse(session_id=session_id, events=events, total=len(events))
        return JSONResponse(body.model_dump())

    async def session_messages(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        events = build_session_timeline(resolved, source="agent")
        return JSONResponse(
            TimelineResponse(
                session_id=session_id, events=events, total=len(events)
            ).model_dump()
        )

    async def session_nika(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        events = build_session_timeline(resolved, source="nika")
        return JSONResponse(
            TimelineResponse(
                session_id=session_id, events=events, total=len(events)
            ).model_dump()
        )

    async def session_scores(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        return JSONResponse(load_scores(resolved).model_dump())

    async def session_raw(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        filename = request.path_params["filename"]
        if filename not in RAW_ALLOWLIST:
            return _error(f"Artifact not allowlisted: {filename}", status=404)
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            data = read_raw_artifact(resolved, filename)
        except FileNotFoundError:
            return _error(
                f"Missing artifact: {filename}", status=404, error_type="NotFound"
            )
        return JSONResponse({"filename": filename, "data": data})

    async def spa_index(_request: Request) -> Response:
        index = _WWW_DIST / "index.html"
        if not index.is_file():
            return _error(
                "Inspect UI assets missing. Build src/nika/view/www or reinstall nika.",
                status=503,
                error_type="AssetsMissing",
            )
        return FileResponse(index)

    routes: list[Any] = [
        Route("/api/health", health),
        Route("/api/roots", roots),
        Route("/api/sessions", sessions),
        Route("/api/sessions/{session_id:path}/timeline", session_timeline),
        Route("/api/sessions/{session_id:path}/messages", session_messages),
        Route("/api/sessions/{session_id:path}/nika", session_nika),
        Route("/api/sessions/{session_id:path}/scores", session_scores),
        Route("/api/sessions/{session_id:path}/raw/{filename}", session_raw),
        Route("/api/sessions/{session_id:path}", session_detail),
    ]

    if _WWW_DIST.is_dir():
        assets_dir = _WWW_DIST / "assets"
        if assets_dir.is_dir():
            routes.append(
                Mount("/assets", StaticFiles(directory=assets_dir), name="assets")
            )
        routes.append(Route("/", spa_index))
        routes.append(Route("/{path:path}", spa_index))

    return Starlette(routes=routes)
