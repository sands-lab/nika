"""Starlette app for the local session viewer."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from nika.inspect.catalog import (
    RAW_ALLOWLIST,
    RUNNING_HIDDEN_ARTIFACTS,
    AmbiguousSessionError,
    aggregate_benchmark_runs,
    build_session_facets,
    delete_session_result,
    detail_session_dir,
    discover_sessions,
    filter_sessions,
    find_session_dir,
    list_browse_entries,
    list_selectable_roots,
    is_session_running,
    load_scores,
    read_raw_artifact,
    resolve_results_selection,
)
from nika.inspect.live_progress import list_benchmark_progress
from nika.inspect.models import (
    BenchmarkProgressResponse,
    BrowseResponse,
    ResultsRootsResponse,
    SessionListResponse,
    TimelineResponse,
)
from nika.inspect.timeline import build_session_timeline

_WWW_DIST = Path(__file__).resolve().parent / "www" / "dist"

_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str | None) -> str | None:
    """Hostname from a ``Host`` header or ``Origin`` URL (IPv6 brackets stripped)."""
    if not value:
        return None
    netloc = value if "://" in value else f"//{value}"
    try:
        return urlsplit(netloc).hostname
    except ValueError:
        return None


def _error(
    message: str, *, status: int = 400, error_type: str = "Error"
) -> JSONResponse:
    return JSONResponse(
        {"error": message, "error_type": error_type},
        status_code=status,
    )


def create_inspect_app(
    *, results_root: Path, bind_host: str | None = None
) -> Starlette:
    """Build the view API + static SPA.

    ``bind_host`` is the address ``serve_inspect`` listens on:

    - loopback: only loopback ``Host`` headers are served (blocks DNS rebinding);
    - all interfaces (``0.0.0.0`` / ``::``): read-only, limited to the base root;
    - ``None`` (embedding/tests): no host policy.

    Cross-origin DELETE is always refused.
    """

    base_root = Path(results_root).resolve()
    loopback_only = bind_host in _LOOPBACK_BIND_HOSTS
    remote = bind_host is not None and not loopback_only

    async def _guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = _hostname(request.headers.get("host"))
        if loopback_only and host not in _LOOPBACK_HOSTNAMES:
            return _error("Host not allowed", status=403, error_type="Forbidden")
        if request.method == "DELETE":
            if remote:
                return _error(
                    "Delete is disabled when inspect listens on all interfaces",
                    status=403,
                    error_type="Forbidden",
                )
            origin = request.headers.get("origin")
            if origin is not None and _hostname(origin) != host:
                return _error(
                    "Cross-origin delete refused", status=403, error_type="Forbidden"
                )
        return await call_next(request)

    def _active_root(request: Request) -> Path | JSONResponse:
        try:
            return resolve_results_selection(
                base_root, request.query_params.get("root"), allow_outside=not remote
            )
        except ValueError as exc:
            return _error(str(exc), status=400)

    def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "results_root": str(base_root),
                "roots": list_selectable_roots(base_root),
            }
        )

    def roots(request: Request) -> JSONResponse:
        selected = request.query_params.get("root") or "."
        try:
            active = resolve_results_selection(
                base_root, selected, allow_outside=not remote
            )
        except ValueError as exc:
            return _error(str(exc), status=400)
        body = ResultsRootsResponse(
            base_root=str(base_root),
            selected_root=selected if selected not in {"", None} else ".",
            results_root=str(active),
            roots=list_selectable_roots(base_root),
        )
        return JSONResponse(body.model_dump())

    def browse(request: Request) -> JSONResponse:
        selected = request.query_params.get("path") or request.query_params.get("root")
        try:
            body = BrowseResponse(
                **list_browse_entries(
                    base_root, path=selected, allow_outside=not remote
                )
            )
        except ValueError as exc:
            return _error(str(exc), status=400)
        return JSONResponse(body.model_dump())

    def sessions(request: Request) -> JSONResponse:
        active = _active_root(request)
        if isinstance(active, JSONResponse):
            return active
        status = request.query_params.get("status", "all")
        if status not in {"running", "finished", "aborted", "error", "all"}:
            return _error("status must be running, finished, aborted, error, or all")
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

    def benchmark_progress(request: Request) -> JSONResponse:
        """Read-only live suite progress from ``runtime/benchmark_runs``."""
        status_raw = request.query_params.get("status", "running")
        status_filter: str | None
        if status_raw in {"", "all"}:
            status_filter = None
        else:
            status_filter = status_raw
        under: Path | None = None
        under_raw = request.query_params.get("under")
        if under_raw:
            candidate = Path(under_raw)
            if not candidate.is_absolute():
                candidate = (base_root / candidate).resolve()
            else:
                candidate = candidate.resolve()
            if remote:
                try:
                    candidate.relative_to(base_root)
                except ValueError:
                    return _error(f"Results folder escapes root: {under_raw}")
            under = candidate
        else:
            active = _active_root(request)
            if isinstance(active, JSONResponse):
                return active
            under = active
        runs = list_benchmark_progress(status=status_filter, under=under)
        body = BenchmarkProgressResponse(runs=runs, total=len(runs))
        return JSONResponse(body.model_dump())

    def _resolve(request: Request, session_id: str) -> Path | JSONResponse:
        active = _active_root(request)
        if isinstance(active, JSONResponse):
            return active
        try:
            session_dir = find_session_dir(session_id, results_root=active)
        except AmbiguousSessionError as exc:
            return _error(str(exc), status=409, error_type="Ambiguous")
        if session_dir is None:
            return _error(
                f"Session not found: {session_id}", status=404, error_type="NotFound"
            )
        return session_dir

    def session_detail(request: Request) -> JSONResponse:
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

    def session_delete(request: Request) -> JSONResponse:
        """Delete one session result directory under the active results root."""
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        active = _active_root(request)
        assert not isinstance(active, JSONResponse)
        try:
            deleted = delete_session_result(resolved, results_root=active)
        except FileNotFoundError as exc:
            return _error(str(exc), status=404, error_type="NotFound")
        except ValueError as exc:
            return _error(str(exc), status=409, error_type="Conflict")
        return JSONResponse(
            {
                "deleted": True,
                "session_id": session_id,
                "session_dir": str(deleted),
            }
        )

    def session_timeline(request: Request) -> JSONResponse:
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

    def session_messages(request: Request) -> JSONResponse:
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

    def session_nika(request: Request) -> JSONResponse:
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

    def session_scores(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        return JSONResponse(load_scores(resolved).model_dump())

    def session_raw(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        filename = request.path_params["filename"]
        if filename not in RAW_ALLOWLIST:
            return _error(f"Artifact not allowlisted: {filename}", status=404)
        resolved = _resolve(request, session_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        # Agents on the same host could read the answer key mid-run.
        if filename in RUNNING_HIDDEN_ARTIFACTS and is_session_running(resolved):
            return _error(
                f"{filename} is hidden while the session is running",
                status=403,
                error_type="Forbidden",
            )
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
                "Inspect UI assets missing. Build src/nika/inspect/www or reinstall nika.",
                status=503,
                error_type="AssetsMissing",
            )
        return FileResponse(index)

    routes: list[Any] = [
        Route("/api/health", health),
        Route("/api/roots", roots),
        Route("/api/browse", browse),
        Route("/api/sessions", sessions),
        Route("/api/benchmark-progress", benchmark_progress),
        Route("/api/sessions/{session_id:path}/timeline", session_timeline),
        Route("/api/sessions/{session_id:path}/messages", session_messages),
        Route("/api/sessions/{session_id:path}/nika", session_nika),
        Route("/api/sessions/{session_id:path}/scores", session_scores),
        Route("/api/sessions/{session_id:path}/raw/{filename}", session_raw),
        Route("/api/sessions/{session_id:path}", session_detail, methods=["GET"]),
        Route("/api/sessions/{session_id:path}", session_delete, methods=["DELETE"]),
    ]

    if _WWW_DIST.is_dir():
        assets_dir = _WWW_DIST / "assets"
        if assets_dir.is_dir():
            routes.append(
                Mount("/assets", StaticFiles(directory=assets_dir), name="assets")
            )
        routes.append(Route("/", spa_index))
        routes.append(Route("/{path:path}", spa_index))

    return Starlette(
        routes=routes, middleware=[Middleware(BaseHTTPMiddleware, dispatch=_guard)]
    )
