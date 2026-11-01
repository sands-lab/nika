"""Parse the far host end of a Containerlab wire from ``tc filter show``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_wire_partner_reads_mirred_redirect_device() -> None:
    controller = HostTcController(object())
    controller._run = lambda *args: _MIRRED_INGRESS
    assert controller.wire_partner("nw111111111111") == "nw12ab34567890"


def test_wire_partner_without_redirect_is_absent() -> None:
    controller = HostTcController(object())
    controller._run = lambda *args: "filter protocol all pref 1 matchall chain 0\n"
    assert controller.wire_partner("nw111111111111") is None


_PID_PATH = "/tmp/nika-link-flap-labkey-peer.pid"


def test_owned_flap_pid_refuses_pid_one_and_non_numeric() -> None:
    cmdline = f"/bin/sh -c PID_FILE={_PID_PATH} while true"
    assert owned_flap_pid("1\n", cmdline, _PID_PATH) is None
    assert owned_flap_pid("0", cmdline, _PID_PATH) is None
    assert owned_flap_pid("-1", cmdline, _PID_PATH) is None
    assert owned_flap_pid("init", cmdline, _PID_PATH) is None


def test_owned_flap_pid_requires_pid_path_in_cmdline() -> None:
    assert owned_flap_pid("42", "sshd -D", _PID_PATH) is None
    assert (
        owned_flap_pid("42", f"/bin/sh\0-c\0PID_FILE={_PID_PATH} flap", _PID_PATH) == 42
    )


def test_stop_owned_flap_worker_does_not_kill_pid_one() -> None:
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> str:
        calls.append(args)
        if args[0] == "cat" and args[1] == _PID_PATH:
            return "1\n"
        if args[0] == "rm":
            return ""
        raise RuntimeCapabilityError("unexpected")

    stop_owned_flap_worker(run, _PID_PATH)
    assert all(call[0] != "kill" for call in calls)
    assert ("rm", "-f", _PID_PATH) in calls


def test_stop_owned_flap_worker_does_not_kill_unrelated_cmdline() -> None:
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> str:
        calls.append(args)
        if args[0] == "cat" and args[1] == _PID_PATH:
            return "4242\n"
        if args[0] == "cat" and args[1] == "/proc/4242/cmdline":
            return "sshd -D"
        if args[0] == "rm":
            return ""
        raise RuntimeCapabilityError(f"unexpected {args}")

    stop_owned_flap_worker(run, _PID_PATH)
    assert all(call[0] != "kill" for call in calls)


def test_stop_owned_flap_worker_kills_matching_flap_script() -> None:
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> str:
        calls.append(args)
        if args[0] == "cat" and args[1] == _PID_PATH:
            return "4242\n"
        if args[0] == "cat" and args[1] == "/proc/4242/cmdline":
            return f"/bin/sh -c PID_FILE={_PID_PATH} while true"
        if args[0] in {"kill", "rm"}:
            return ""
        raise RuntimeCapabilityError(f"unexpected {args}")

    stop_owned_flap_worker(run, _PID_PATH)
    assert ("kill", "-KILL", "--", "-4242") in calls


def test_set_link_down_raises_when_host_peer_is_missing() -> None:
    controller = HostTcController(SimpleNamespace(lab_name="lab"))
    controller.peer_name = MagicMock(
        side_effect=RuntimeCapabilityError("host veth is absent")
    )
    with pytest.raises(RuntimeCapabilityError, match="absent"):
        controller.set_link_down("leaf1", "eth1")


def test_set_tbf_raises_when_host_peer_is_missing() -> None:
    controller = HostTcController(SimpleNamespace(lab_name="lab"))
    controller.egress_dev = MagicMock(
        side_effect=RuntimeCapabilityError("host veth is absent")
    )
    controller._run = MagicMock(side_effect=AssertionError("must not mutate the node"))
    with pytest.raises(RuntimeCapabilityError, match="absent"):
        controller.set_tbf("leaf1", "eth1", "512kbit", "16kb", "32kb")
    controller._run.assert_not_called()
