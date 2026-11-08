"""Parse the far host end of a Containerlab wire from ``tc filter show``."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nika.runtime.shared.processes import process_identity
from nika.service.containerlab.host_tc import HostTcController

pytestmark = pytest.mark.unit

# iproute2 6.1 ``tc filter show dev <peer> ingress`` after
# ``tc filter replace ... matchall action mirred egress redirect dev <far>``.
_MIRRED_INGRESS = """\
filter protocol all pref 1 matchall chain 0
filter protocol all pref 1 matchall chain 0 handle 0x1
  action order 1: mirred (Egress Redirect to device nw12ab34567890) stolen
	index 1 ref 1 bind 1
"""


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_MIRRED_INGRESS, "nw12ab34567890"),
        ("filter protocol all pref 1 matchall chain 0\n", None),
    ],
)
def test_wire_partner_reads_mirred_redirect_device(
    output: str, expected: str | None
) -> None:
    controller = HostTcController(object())
    controller._run = lambda *args: output
    assert controller.wire_partner("nw111111111111") == expected


@pytest.mark.parametrize("all_labs", [False, True])
def test_cleanup_stops_owned_worker_and_preserves_stale_identity(
    monkeypatch, tmp_path, all_labs
) -> None:
    prefix = str(tmp_path / "nika-link-flap-")
    monkeypatch.setattr(
        HostTcController, "_flap_pid_prefix", staticmethod(lambda lab: prefix)
    )
    workers = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        for _ in range(2)
    ]
    try:
        for name, worker in zip(("owned", "stale"), workers, strict=True):
            identity = process_identity(worker.pid)
            if name == "stale":
                identity[1] += 1
            (tmp_path / f"nika-link-flap-{name}.pid").write_text(json.dumps(identity))

        def run(*args):
            # Scope the explicit global pattern to this test's owned directory.
            args = tuple(arg.replace("/tmp/nika-link-flap-", prefix) for arg in args)
            return subprocess.check_output(args, text=True)

        monkeypatch.setattr(HostTcController, "_run", staticmethod(run))
        HostTcController.cleanup_lab("test_cleanup", all_labs=all_labs)
        assert workers[0].wait(timeout=5) != 0
        assert workers[1].poll() is None
        assert not list(tmp_path.glob("*.pid"))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)


def test_missing_lab_does_not_stop_other_workers(monkeypatch) -> None:
    def run(*args):
        raise AssertionError("A missing lab must not run host cleanup")

    monkeypatch.setattr(HostTcController, "_run", staticmethod(run))
    HostTcController.cleanup_lab(None)
