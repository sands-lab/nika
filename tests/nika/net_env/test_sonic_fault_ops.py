"""SONiC failure operations and CLI access issue the right router commands (no Docker)."""

from __future__ import annotations

import json
import shlex

import pytest

from nika.net_env.utils.sonic.fault_ops import SonicFaultOps
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


def test_originate_bgp_prefix_raises_when_loopback_never_appears() -> None:
    ops = SonicFaultOps(FakeRuntime({"show running-config": RUNNING}), "leaf1")
    with pytest.raises(RuntimeError, match="Loopback1 on leaf1 did not get"):
        ops.originate_bgp_prefix("203.0.113.0/24")


def test_originate_bgp_prefix_advertises_after_loopback_appears() -> None:
    runtime = FakeRuntime(
        {
            "show running-config": RUNNING,
            "ip -4 addr show dev Loopback1": "present",
        }
    )
    ops = SonicFaultOps(runtime, "leaf1")
    ops.originate_bgp_prefix("203.0.113.0/24")
    assert any("network 203.0.113.0/24" in cmd for cmd in runtime.commands)
    assert any("write memory" in cmd for cmd in runtime.commands)


def test_wait_port_raises_when_the_port_never_appears() -> None:
    ops = SonicFaultOps(FakeRuntime(), "leaf1")
    with pytest.raises(RuntimeError, match="did not appear"):
        ops._wait_port("Ethernet0", present=True)


def test_wait_port_raises_when_the_port_never_disappears() -> None:
    runtime = FakeRuntime({"ip link show Ethernet0": "present"})
    ops = SonicFaultOps(runtime, "leaf1")
    with pytest.raises(RuntimeError, match="did not disappear"):
        ops._wait_port("Ethernet0", present=False)


class _ReattachRuntime:
    """CONFIG_DB rows plus a TAP that follows PORT delete/restore."""

    _PORT = (
        "admin_status\nup\nalias\neth1\nindex\n0\n"
        "lanes\n1,2,3,4\nmtu\n9100\nspeed\n40000"
    )
    _INTERFACE = "NULL\nNULL"

    def __init__(self, *, kernel_present: bool = True) -> None:
        self.commands: list[str] = []
        self.kernel_present = kernel_present
        self.tap_present = True

    def exec(self, node: str, command: str, timeout: float = 30) -> str:
        del node, timeout
        self.commands.append(command)
        if "ip link show eth1" in command:
            return "present" if self.kernel_present else ""
        if "keys 'PORT|Ethernet0'" in command:
            return "PORT|Ethernet0"
        if "keys 'INTERFACE|Ethernet0|*'" in command:
            return "INTERFACE|Ethernet0|10.0.0.0/31"
        if "keys 'INTERFACE|Ethernet0'" in command:
            return "INTERFACE|Ethernet0"
        if "hgetall 'PORT|Ethernet0'" in command:
            return self._PORT
        if "hgetall" in command:
            return self._INTERFACE
        if command.startswith("redis-cli -n 4 del 'PORT|Ethernet0'"):
            self.tap_present = False
            return ""
        if command.startswith("redis-cli -n 4 hset 'PORT|Ethernet0'"):
            self.tap_present = True
            return ""
        if "ip link show Ethernet0" in command or "ip link show 'Ethernet0'" in command:
            return "present" if self.tap_present else ""
        return ""


def test_reattach_recreates_config_db_port_when_the_veth_is_back() -> None:
    runtime = _ReattachRuntime()
    SonicFaultOps(runtime, "leaf1").reattach_interface("eth1")
    assert runtime.tap_present
    deletes = [cmd for cmd in runtime.commands if cmd.startswith("redis-cli -n 4 del ")]
    restores = [
        cmd for cmd in runtime.commands if cmd.startswith("redis-cli -n 4 hset ")
    ]
    assert deletes == [
        "redis-cli -n 4 del 'INTERFACE|Ethernet0|10.0.0.0/31'",
        "redis-cli -n 4 del 'INTERFACE|Ethernet0'",
        "redis-cli -n 4 del 'PORT|Ethernet0'",
    ]
    assert [cmd.split()[4] for cmd in restores] == [
        "'PORT|Ethernet0'",
        "'INTERFACE|Ethernet0'",
        "'INTERFACE|Ethernet0|10.0.0.0/31'",
    ]
    assert "admin_status" in restores[0] and "up" in restores[0]


def test_reattach_raises_when_the_veth_is_missing() -> None:
    runtime = _ReattachRuntime(kernel_present=False)
    with pytest.raises(RuntimeError, match="eth1 on leaf1 is missing"):
        SonicFaultOps(runtime, "leaf1").reattach_interface("eth1")
    assert not any("redis-cli -n 4 del " in cmd for cmd in runtime.commands)


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
