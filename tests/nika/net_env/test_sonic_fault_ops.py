"""SONiC failure operations and CLI access issue the right router commands (no Docker)."""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from nika.net_env.utils.sonic.fault_ops import SonicFaultOps
from nika.problems.routing_control_plane.ospf import (
    OSPFAreaMisconfig,
    OSPFAreaMisconfigParams,
)
from nika.service.containerlab.sonic_api import SONIC_MARKER, sonic_cli_command

pytestmark = pytest.mark.unit


class FakeRuntime:
    """Record commands and answer them from ``replies`` (substring -> output)."""

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.commands: list[str] = []

    def exec(self, node: str, command: str, timeout: float = 30) -> str:
        self.commands.append(command)
        for needle, output in self.replies.items():
            if needle in command:
                return output
        return ""


RUNNING = """\
frr version 10.3
router bgp 65001
 neighbor 10.0.0.1 remote-as 65056
 address-family ipv4 unicast
  network 10.0.0.24/31
 exit-address-family
exit
!
router ospf
 network 10.0.0.0/31 area 0.0.0.0
exit
!
"""


def _save_commands(commands: list[str]) -> list[str]:
    return [cmd for cmd in commands if "write memory" in cmd]


def test_bgp_state_is_read_from_frr() -> None:
    runtime = FakeRuntime({"show running-config": RUNNING})
    ops = SonicFaultOps(runtime, "leaf1")
    assert ops.bgp_asn() == 65001
    assert ops.bgp_networks() == ["10.0.0.24/31"]
    ops.withdraw_bgp_network("10.0.0.24/31")
    assert shlex.split(runtime.commands[-2]) == [
        "vtysh",
        *("-c", "configure terminal"),
        *("-c", "router bgp 65001"),
        *("-c", "address-family ipv4 unicast"),
        *("-c", "no network 10.0.0.24/31"),
    ]


def test_frr_faults_persist_without_backup_files() -> None:
    runtime = FakeRuntime({"show running-config": RUNNING})
    ops = SonicFaultOps(runtime, "leaf1")
    ops.withdraw_bgp_network("10.0.0.24/31")
    ops.renumber_ospf_area("0.0.0.0", "0.0.0.66")
    ops.add_blackhole_route("203.0.113.0/24")
    saves = _save_commands(runtime.commands)
    assert len(saves) == 3
    assert all("rm -f /etc/frr/*.sav" in cmd for cmd in saves)


def test_wait_port_raises_when_the_port_never_appears() -> None:
    ops = SonicFaultOps(FakeRuntime(), "leaf1")
    with pytest.raises(RuntimeError, match="did not appear"):
        ops._wait_port("Ethernet0", present=True)


def test_wait_port_raises_when_the_port_never_disappears() -> None:
    runtime = FakeRuntime({"ip link show Ethernet0": "present"})
    ops = SonicFaultOps(runtime, "leaf1")
    with pytest.raises(RuntimeError, match="did not disappear"):
        ops._wait_port("Ethernet0", present=False)


def test_ospf_area_native_verify_requires_frr_conf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_conf = "router ospf\n network 10.0.0.0/31 area 0.0.0.0\nexit\n"
    ops = SimpleNamespace(
        ospf_interface_areas=lambda: ["0.0.0.66"],
        supports=lambda name: name == "frr_shell",
        frr_shell=lambda _cmd: file_conf,
    )
    monkeypatch.setattr(
        "nika.problems.routing_control_plane.ospf.native_fault_ops",
        lambda *_a, **_k: ops,
    )
    problem = OSPFAreaMisconfig(None)
    problem._orig_areas = {"0.0.0.0"}
    result = problem.verify_fault(OSPFAreaMisconfigParams(host_name="berlin"))
    assert result["verified"] is False
    assert result["details"]["file_areas"] == ["0.0.0.0"]
    assert result["details"]["running_areas"] == ["0.0.0.66"]

    ops.frr_shell = lambda _cmd: (
        "router ospf\n network 10.0.0.0/31 area 0.0.0.66\nexit\n"
    )
    assert problem.verify_fault(OSPFAreaMisconfigParams(host_name="berlin"))["verified"]


def test_ospf_interfaces_are_named_by_container_interface() -> None:
    interfaces = {"interfaces": {"Ethernet4": {"ipAddress": "10.0.0.0"}}}
    neighbors = {
        "neighbors": {
            "30.0.0.3": [{"nbrState": "Full/-", "ifaceName": "Ethernet4:10.0.0.0"}]
        }
    }
    runtime = FakeRuntime(
        {
            "show ip ospf interface json": json.dumps(interfaces),
            "show ip ospf neighbor json": json.dumps(neighbors),
        }
    )
    ops = SonicFaultOps(runtime, "leaf1")
    assert ops.ospf_interfaces() == ["eth2"]
    assert ops.ospf_neighbor_interfaces() == ["eth2"]


def test_port_mtu_goes_to_config_db_and_must_reach_the_kernel() -> None:
    runtime = FakeRuntime({"hget 'PORT|Ethernet4' mtu": "500", "/mtu": "500"})
    ops = SonicFaultOps(runtime, "leaf1")
    ops.set_interface_mtu("eth2", 500)
    assert runtime.commands[0] == "redis-cli -n 4 hset 'PORT|Ethernet4' mtu 500"
    assert ops.interface_mtu("eth2") == 500
    # portmgrd has not applied it yet: the kernel port keeps the old MTU.
    stale = FakeRuntime({"hget 'PORT|Ethernet4' mtu": "500", "/mtu": "9100"})
    assert SonicFaultOps(stale, "leaf1").interface_mtu("Ethernet4") is None


def test_frag_needed_filter_covers_every_chain() -> None:
    runtime = FakeRuntime({"iptables -C": "present"})
    ops = SonicFaultOps(runtime, "leaf1")
    ops.block_icmp_frag_needed()
    assert [cmd.split()[2] for cmd in runtime.commands] == [
        "INPUT",
        "FORWARD",
        "OUTPUT",
    ]
    assert all(
        "--icmp-type fragmentation-needed -j DROP" in c for c in runtime.commands
    )
    assert ops.icmp_frag_needed_blocked()
    assert not SonicFaultOps(FakeRuntime(), "leaf1").icmp_frag_needed_blocked()


@pytest.mark.parametrize(
    ("command", "line"),
    [
        ("show interfaces status", "show interfaces status"),
        ('vtysh -c "show bgp summary"', "vtysh -c 'show bgp summary'"),
        (
            'vtysh -c "show ip bgp | include 10."',
            "vtysh -c 'show ip bgp | include 10.'",
        ),
        (
            "redis-cli -n 4 hget 'PORT|Ethernet0' mtu",
            "redis-cli -n 4 hget 'PORT|Ethernet0' mtu",
        ),
    ],
)
def test_sonic_cli_runs_one_quoted_command_on_sonic_only(
    command: str, line: str
) -> None:
    script = sonic_cli_command(command)
    assert script.startswith(f"if [ ! -e {SONIC_MARKER} ]")
    assert script.endswith(f"; {line}")


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("ls /", "start with one of"),
        ("", "start with one of"),
        ("show ip route | grep 10", "without shell operators"),
        ("show version ; reboot", "without shell operators"),
        ("show 'unterminated", "Cannot parse"),
    ],
)
def test_sonic_cli_rejects_other_programs_and_shell_operators(
    command: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        sonic_cli_command(command)


def test_sonic_cli_quotes_substitutions() -> None:
    assert sonic_cli_command("show $(reboot)").endswith("; show '$(reboot)'")
