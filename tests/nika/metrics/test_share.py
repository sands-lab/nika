"""Shared run dashboard links (``nika.metrics.share``)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from nika.metrics import share
from nika.workflows.benchmark.run_progress import annotate_progress, write_progress

pytestmark = pytest.mark.unit


def test_link_opens_the_run_from_its_start() -> None:
    started = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)

    url = urlparse(share.dashboard_link("https://a-b.trycloudflare.com", "r1", started))

    assert url.netloc == "a-b.trycloudflare.com"
    assert url.path == share.DASHBOARD_PATH
    query = parse_qs(url.query)
    assert query["var-run_id"] == ["r1"]
    assert int(query["from"][0]) < started.timestamp() * 1000


def test_sharing_off_starts_no_tunnel(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    request.addfinalizer(reset_run_config)
    set_run_config(RunConfig())  # built-in defaults, not the checkout's config
    with patch.object(share, "_client") as client:
        record = share.share_run("r1", tmp_path, started_at=datetime.now(timezone.utc))

    assert record is None
    client.assert_not_called()
    assert not (tmp_path / share.DASHBOARD_FILENAME).exists()


def test_links_report_which_tunnels_still_run(tmp_path: Path) -> None:
    for run_id in ("live", "gone"):
        write_progress(run_id, result_dir=tmp_path / run_id)
        annotate_progress(
            run_id,
            {
                "dashboard_url": f"https://{run_id}.trycloudflare.com/d",
                "dashboard_expires_at": "2026-11-02T09:00:00+00:00",
            },
        )
    write_progress("unshared", result_dir=tmp_path / "unshared")

    with patch.object(
        share, "list_shares", return_value=[{"run_id": "live", "expires_at": None}]
    ):
        links = share.run_links()

    assert {link["run_id"]: link["live"] for link in links} == {
        "live": True,
        "gone": False,
    }
