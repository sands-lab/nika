"""Public dashboard links for benchmark runs, through Cloudflare Quick Tunnels.

Each benchmark run gets its own ``cloudflared`` container that forwards a
random ``trycloudflare.com`` address to the local Grafana. A Quick Tunnel needs
no Cloudflare account. BusyBox ``timeout`` ends the tunnel after the configured
lifetime; the container never restarts and Docker removes it once it exits, so
the link stops working for good.

The link is saved in the run's result directory (``dashboard.json``) and in its
progress document, so ``nika observability links`` can show it again later.
"""

from __future__ import annotations

import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from nika.metrics.stack import GRAFANA_URL
from nika.workflows.benchmark.run_progress import annotate_progress

CLOUDFLARED_VERSION = "2026.10.3"
TUNNEL_IMAGE = f"nika-metrics-tunnel:{CLOUDFLARED_VERSION}"
# The cloudflared image has no shell; BusyBox brings ``timeout``.
_DOCKERFILE = f"""\
FROM busybox:1.37.0-musl AS busybox
FROM cloudflare/cloudflared:{CLOUDFLARED_VERSION}
COPY --from=busybox /bin/busybox /bin/busybox
"""
SHARE_LABEL = "nika.metrics.share"
EXPIRES_LABEL = "nika.metrics.share.expires"
DASHBOARD_FILENAME = "dashboard.json"
DASHBOARD_PATH = "/d/nika-benchmark/nika-benchmark"

# The tunnel's address; ``api.trycloudflare.com`` appears in error lines.
_URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
# Quick Tunnel requests sometimes take tens of seconds.
_URL_TIMEOUT_SEC = 90.0


def container_name(run_id: str) -> str:
    return f"nika-share-{run_id}"


def dashboard_link(base_url: str, run_id: str, started_at: datetime) -> str:
    """Dashboard URL showing ``run_id`` from its start until now."""
    query = urlencode(
        {
            "var-run_id": run_id,
            "from": int(started_at.timestamp() * 1000) - 60_000,
            "to": "now",
        }
    )
    return f"{base_url}{DASHBOARD_PATH}?{query}"


def _client() -> Any:
    from nika.runtime.shared.containers import docker_client

    return docker_client()


def _ensure_image() -> None:
    import docker.errors

    client = _client()
    try:
        client.images.get(TUNNEL_IMAGE)
    except docker.errors.ImageNotFound:
        client.images.build(
            fileobj=io.BytesIO(_DOCKERFILE.encode()), tag=TUNNEL_IMAGE, rm=True
        )


def _live_container(run_id: str) -> Any | None:
    import docker.errors

    try:
        container = _client().containers.get(container_name(run_id))
    except docker.errors.NotFound:
        return None
    return container if container.status == "running" else None


def read_dashboard(result_dir: str | Path) -> dict[str, Any] | None:
    """The run's saved link record, if any."""
    try:
        data = json.loads((Path(result_dir) / DASHBOARD_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_live(record: dict[str, Any]) -> bool:
    """True while the record's tunnel is still running."""
    return _live_container(str(record.get("run_id"))) is not None


def share_run(
    run_id: str,
    result_dir: str | Path,
    *,
    started_at: datetime,
    run_name: str | None = None,
) -> dict[str, Any] | None:
    """Open (or reuse) the public link of ``run_id``; None when sharing is off.

    Raises ``RuntimeError`` when the tunnel does not come up.
    """
    from nika.run_config.loader import get_run_config

    settings = get_run_config().nika.metrics.share
    if not settings.enabled:
        return None
    existing = read_dashboard(result_dir)
    if existing and existing.get("run_id") == run_id and is_live(existing):
        return existing

    _ensure_image()
    client = _client()
    stale = _live_container(run_id)
    if stale is not None:
        stale.remove(force=True)

    created = datetime.now(timezone.utc).replace(microsecond=0)
    expires = created + timedelta(days=settings.ttl_days)
    command = [
        "/bin/busybox",
        "timeout",
        str(settings.ttl_days * 86400),
        "cloudflared",
        "--no-autoupdate",
        "tunnel",
        "--url",
        GRAFANA_URL,
    ]
    for email in settings.allowed_emails:
        command += ["--allowed-mail", email]
    container = client.containers.run(
        TUNNEL_IMAGE,
        entrypoint=command[:1],
        command=command[1:],
        name=container_name(run_id),
        detach=True,
        auto_remove=True,
        network_mode="host",
        restart_policy={"Name": "no"},
        labels={SHARE_LABEL: run_id, EXPIRES_LABEL: expires.isoformat()},
    )

    deadline = time.monotonic() + _URL_TIMEOUT_SEC
    tunnel_url = None
    logs = ""
    while tunnel_url is None and time.monotonic() < deadline:
        try:
            logs = container.logs().decode(errors="replace")
        except Exception:  # noqa: BLE001 — cloudflared exited; Docker removed it
            break
        match = _URL_RE.search(logs)
        if match:
            tunnel_url = match.group(0)
        else:
            time.sleep(0.5)
    if tunnel_url is None:
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001 — already gone
            pass
        errors = [line for line in logs.splitlines() if " ERR " in line]
        detail = errors[-1].split(" ERR ", 1)[1] if errors else "no tunnel address"
        raise RuntimeError(
            f"Cloudflare tunnel did not start ({detail}); check that this host "
            "can reach Cloudflare (outbound port 7844)"
        )

    record = {
        "run_id": run_id,
        "run_name": run_name or run_id,
        "url": dashboard_link(tunnel_url, run_id, started_at),
        "tunnel_url": tunnel_url,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "access": "email" if settings.allowed_emails else "link",
    }
    path = Path(result_dir) / DASHBOARD_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    annotate_progress(
        run_id,
        {"dashboard_url": record["url"], "dashboard_expires_at": expires.isoformat()},
    )
    return record


def list_shares() -> list[dict[str, Any]]:
    """Running share tunnels on this host: run id and expiry."""
    try:
        containers = _client().containers.list(filters={"label": SHARE_LABEL})
    except Exception:  # noqa: BLE001 — no Docker means no shares
        return []
    return [
        {"run_id": c.labels[SHARE_LABEL], "expires_at": c.labels.get(EXPIRES_LABEL)}
        for c in containers
    ]


def unshare(run_id: str) -> bool:
    """Stop the run's tunnel now; return whether one was running."""
    container = _live_container(run_id)
    if container is None:
        return False
    container.remove(force=True)
    return True


def stop_all_shares() -> int:
    """Stop every share tunnel on this host; return how many were stopped."""
    stopped = 0
    for share in list_shares():
        stopped += unshare(share["run_id"])
    return stopped


def run_links() -> list[dict[str, Any]]:
    """Dashboard links of this checkout's runs, newest first, with live state."""
    from nika.workflows.benchmark.run_progress import list_progress_docs

    live = {share["run_id"] for share in list_shares()}
    links = [
        {
            "run_id": str(doc["run_id"]),
            "run_name": doc.get("run_name") or str(doc["run_id"]),
            "url": doc["dashboard_url"],
            "expires_at": doc.get("dashboard_expires_at"),
            "live": str(doc["run_id"]) in live,
            "status": doc.get("status"),
            "completed": doc.get("completed_trials"),
            "total": doc.get("total_trials"),
        }
        for doc in list_progress_docs()
        if doc.get("dashboard_url")
    ]
    links.sort(key=lambda link: str(link["expires_at"] or ""), reverse=True)
    return links
