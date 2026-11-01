"""Parse the far host end of a Containerlab wire from ``tc filter show``."""

from __future__ import annotations

import pytest

from nika.runtime.base import RuntimeCapabilityError
from nika.service.containerlab.host_tc import (
    HostTcController,
    owned_flap_pid,
    stop_owned_flap_worker,
)

pytestmark = pytest.mark.unit

# iproute2 6.1 ``tc filter show dev <peer> ingress`` after
# ``tc filter replace ... matchall action mirred egress redirect dev <far>``.
_MIRRED_INGRESS = """\
filter protocol all pref 1 matchall chain 0
filter protocol all pref 1 matchall chain 0 handle 0x1
  action order 1: mirred (Egress Redirect to device nw12ab34567890) stolen
	index 1 ref 1 bind 1
"""
_PID_PATH = "/tmp/nika-link-flap-labkey-peer.pid"
_FLAP_CMDLINE = f"/bin/sh -c PID_FILE={_PID_PATH} while true"


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


@pytest.mark.parametrize(
    ("pid", "cmdline", "expected"),
    [
        ("1\n", _FLAP_CMDLINE, None),
        ("0", _FLAP_CMDLINE, None),
        ("-1", _FLAP_CMDLINE, None),
        ("init", _FLAP_CMDLINE, None),
        ("42", "sshd -D", None),
        ("42", f"/bin/sh\0-c\0PID_FILE={_PID_PATH} flap", 42),
    ],
)
def test_owned_flap_pid_requires_this_lab_worker(
    pid: str, cmdline: str, expected: int | None
) -> None:
    assert owned_flap_pid(pid, cmdline, _PID_PATH) == expected


@pytest.mark.parametrize(
    ("pid_text", "cmdline", "killed"),
    [
        ("1\n", None, False),
        ("4242\n", "sshd -D", False),
        ("4242\n", _FLAP_CMDLINE, True),
    ],
)
def test_stop_owned_flap_worker_kills_only_matching_script(
    pid_text: str, cmdline: str | None, killed: bool
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> str:
        calls.append(args)
        if args[0] == "cat" and args[1] == _PID_PATH:
            return pid_text
        if args[0] == "cat" and args[1].startswith("/proc/"):
            return cmdline or ""
        if args[0] in {"kill", "rm"}:
            return ""
        raise RuntimeCapabilityError(f"unexpected {args}")

    stop_owned_flap_worker(run, _PID_PATH)
    kill = ("kill", "-KILL", "--", "-4242")
    assert (kill in calls) is killed
    if not killed:
        assert all(call[0] != "kill" for call in calls)
