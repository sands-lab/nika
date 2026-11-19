"""Device CLI access and shell quoting contracts."""

import pytest

from nika.mcp.device_read import device_read_argv
from nika.mcp.gateway.access import decide_diagnosis_access

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        "ip route show",
        "ip -4 -br addr show dev eth0",
        "ip -s link show",
        "ip route show table all type blackhole",
        "tc -s filter show dev eth0 egress",
        "nft list ruleset",
        "nft list chain inet filter forward",
        "ss -lntup",
        "ethtool -S eth0",
        "ethtool eth0",
        "bridge fdb show",
        "vtysh -c 'show bgp summary' -c 'show running-config'",
        "ovs-ofctl dump-flows br0",
        "ovs-vsctl list Interface",
    ],
)
def test_device_read_accepts_diagnostic_commands(command):
    assert device_read_argv(command)[0] in {"frr", "ovs"}


@pytest.mark.parametrize(
    "command",
    [
        "nft add table inet selected",
        "nft -f /tmp/rules",
        "ip link set eth0 down",
        "ip route flush table main",
        "tc qdisc add dev eth0 root netem loss 10%",
        "bridge fdb replace aa:bb:cc:dd:ee:ff dev eth0",
        "ss -K",
        "ethtool -K eth0 gro off",
        "wg set wg0 listen-port 51821",
        "conntrack -D -p tcp",
        "ovs-vsctl set Interface eth0 mtu_request=500",
        "ovs-ofctl del-flows br0",
        "vtysh -c 'configure terminal' -c 'interface lo' -c 'description selected'",
        "vtysh -b",
        "ip r s",
        "ping 192.0.2.1",
        "traceroute 192.0.2.1",
    ],
)
def test_device_read_accepts_configuration_and_native_options(command):
    import shlex

    assert device_read_argv(command)[1] == shlex.split(command)


@pytest.mark.parametrize("command", ["", "sh -c id", "python3 -c pass", "ip '"])
def test_device_read_requires_device_program_and_parseable_arguments(command):
    with pytest.raises(ValueError):
        device_read_argv(command)


def test_device_arguments_do_not_execute_shell_substitution(tmp_path):
    import shlex
    import subprocess
    from nika.mcp.device_read import bounded_command

    marker = tmp_path / "escaped"
    command = f"vtysh -c 'description $(touch {marker}); `touch {marker}`'"
    _, argv = device_read_argv(command)
    # Substitute a harmless argv printer for the device CLI at the same boundary.
    output = subprocess.check_output(
        bounded_command(["printf", "%s", *argv[1:]]), shell=True, text=True
    )
    assert f"$(touch {marker})" in output
    assert not marker.exists()
    assert shlex.split(shlex.join(argv)) == argv


@pytest.mark.parametrize("role", ["router", "switch", "infrastructure", None])
def test_wildcard_and_node_id_do_not_grant_shell_on_devices(role):
    decision = decide_diagnosis_access(
        policy={
            "tools": ["*"],
            "node_roles": ["*"],
            "node_ids": ["target"],
            "shell_node_roles": ["*"],
        },
        tool_name="exec_shell",
        arguments={"host_name": "target", "command": "id"},
        node_roles={} if role is None else {"target": role},
    )
    assert (decision.allowed, decision.reason) == (False, "shell_role_not_allowed")


@pytest.mark.parametrize("role", ["host", "server", "service", "controller"])
def test_hosts_servers_and_controllers_keep_shell(role):
    assert decide_diagnosis_access(
        policy={"tools": ["*"], "node_roles": ["*"]},
        tool_name="exec_shell",
        arguments={"host_name": "any-name", "command": "id"},
        node_roles={"any-name": role},
    ).allowed


def test_device_tool_checks_capability_and_validates_entire_batch_before_execution(
    monkeypatch,
):
    from nika.mcp.servers.common import host_server
    from nika.mcp import device_read

    checked = []
    monkeypatch.setattr(
        device_read, "require_device", lambda node, cap: checked.append((node, cap))
    )
    # An invalid command late in a batch cannot partially execute it.
    monkeypatch.setattr(
        host_server, "get_lab_api", lambda: pytest.fail("must not execute")
    )
    result = host_server.device_read("device", ["nft list ruleset", "sh -c id"])
    assert "error" in result
    assert not checked


def test_oracle_device_mapping_preserves_evidence_and_rejects_unsupported_reads():
    from types import SimpleNamespace
    from nika.runtime.spec import NodeIdentity, NodeRole
    from nika.problems.support.oracle import OraclePlaybook, shell, contains
    from nika.problems.support.oracle_access import device_playbook

    env = SimpleNamespace(
        machine_identities={
            "r": NodeIdentity(NodeRole.ROUTER, ("linux", "frr")),
            "h": NodeIdentity(NodeRole.HOST, ("linux",)),
        }
    )
    check = contains("drop")
    read = shell(
        "r", "nft list ruleset 2>&1 | head -60", "Read filters", check, evidence=True
    )
    unsupported = shell(
        "r",
        "cat /proc/sys/net/ipv4/ip_forward",
        "Read forwarding",
        check,
        evidence=True,
    )
    host = shell("h", "ip route", "Read routes")
    plan = device_playbook(OraclePlaybook("finding", [read, unsupported, host]), env)
    assert plan.steps[0].tool == "device_read"
    assert plan.steps[0].args == {"host_name": "r", "commands": ["nft list ruleset"]}
    assert plan.steps[0].expect is check and plan.steps[0].evidence
    assert plan.steps[1:] == [unsupported, host]


@pytest.mark.parametrize(
    "caps,command,allowed",
    [
        (("linux", "frr"), "nft -a list ruleset", True),
        (("linux", "frr", "sonic"), "nft list ruleset", False),
        (("linux", "frr", "sonic"), "vtysh -c 'show ip route'", True),
        (("linux", "routeros"), "ip route show", False),
        (("linux", "ovs"), "ovs-ofctl -O OpenFlow13 dump-flows br0", True),
        (("linux", "bmv2", "p4runtime"), "tc qdisc show", False),
    ],
)
def test_device_reads_follow_declared_capabilities(caps, command, allowed):
    from nika.mcp.device_read import device_read_supported
    from nika.runtime.spec import NodeIdentity, NodeRole

    identity = NodeIdentity(NodeRole.ROUTER, caps)
    assert device_read_supported(identity, device_read_argv(command)[1]) is allowed


def test_server_roles_preserve_shell_without_expanding_host_fault_pools():
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.runtime.spec import NodeRole

    for scenario in (
        "k8s_lab",
        "llmd_lab",
        "campus_lan",
        "enterprise_branch",
        "sdn_l3_clos",
    ):
        env = load_offline_net_env(scenario, "s")
        servers = [
            n for n, i in env.machine_identities.items() if i.role is NodeRole.SERVER
        ]
        assert servers, scenario
        assert not set(servers) & set(env.hosts)
        for node in servers:
            assert decide_diagnosis_access(
                policy={"tools": ["*"], "node_roles": ["*"]},
                tool_name="exec_shell",
                arguments={"host_name": node},
                node_roles={node: "server"},
            ).allowed


def test_device_runtime_and_output_are_bounded(monkeypatch):
    import subprocess
    import time

    from nika.mcp import device_read

    output = subprocess.run(
        device_read.bounded_command(["head", "-c", "100000", "/dev/zero"]),
        shell=True,
        capture_output=True,
        timeout=2,
    )
    assert len(output.stdout) <= device_read.MAX_OUTPUT + 1
    monkeypatch.setattr(device_read, "COMMAND_TIMEOUT", 0.05)
    start = time.monotonic()
    expired = subprocess.run(
        device_read.bounded_command(["sleep", "5"]),
        shell=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert time.monotonic() - start < 2
    assert device_read.TIMEOUT_MARKER in expired.stdout


@pytest.mark.asyncio
async def test_oracle_routes_a_dynamic_router_read_through_the_device_tool():
    from itertools import count
    from types import SimpleNamespace

    from nika.agents.oracle.agent import OracleAgent
    from nika.problems.support.oracle import OracleStep, contains
    from nika.runtime.spec import NodeIdentity, NodeRole

    calls = []

    async def invoke(args):
        calls.append(args)
        return "BGP state = Established"

    agent = OracleAgent.__new__(OracleAgent)
    agent.net_env = SimpleNamespace(
        machine_identities={"r": NodeIdentity(NodeRole.ROUTER, ("linux", "frr"))}
    )
    step = OracleStep(
        "Check the previously observed peer",
        "exec_shell",
        lambda out: {
            "host_name": "r",
            "command": f"vtysh -c 'show bgp neighbors {out['peer']}'",
        },
        contains("Established"),
        evidence=True,
    )
    result = await agent._run_step(
        1,
        step,
        {"device_read": SimpleNamespace(ainvoke=invoke)},
        {"peer": "10.0.0.1"},
        SimpleNamespace(log=lambda *args: None),
        count(1),
    )
    assert result.passed and result.tool == "device_read"
    assert calls == [
        {"host_name": "r", "commands": ["vtysh -c 'show bgp neighbors 10.0.0.1'"]}
    ]


@pytest.mark.parametrize("output", ["x" * 65537, "NIKA_DEVICE_READ_TIMEOUT"])
def test_device_output_limit_and_timeout_are_errors_not_partial_evidence(
    monkeypatch, output
):
    from types import SimpleNamespace

    from nika.mcp import device_read
    from nika.mcp.servers.common import host_server

    monkeypatch.setattr(device_read, "require_device", lambda *args: None)
    monkeypatch.setattr(
        host_server,
        "get_lab_api",
        lambda: SimpleNamespace(exec_cmd=lambda *args, **kwargs: output),
    )
    result = host_server.device_read("r", ["ip route show"])
    assert result["error"] == "tool_execution_error"


@pytest.mark.parametrize(
    "command",
    [
        "ping -c 3 -W 1 -I wg_hq 192.0.2.1",
        "ping -c 2 -W 1 -M do -s 1400 192.0.2.1",
        "traceroute -n -q 1 -w 1 -m 12 192.0.2.1",
        "wg show wg_hq peers",
        "wg show wg_hq latest-handshakes",
        "conntrack -L -p tcp --reply-dst 192.0.2.1",
        "conntrack -S",
    ],
)
def test_router_operational_reads_are_bounded(command):
    device_read_argv(command)


@pytest.mark.parametrize(
    "role,interface,allowed",
    [
        ("router", "eth1", True),
        ("switch", "eth1", True),
        ("router", "eth1;id", False),
        ("router", "eth0", False),
        ("infrastructure", "eth1", False),
        ("host", "eth1", False),
    ],
)
def test_carrier_read_requires_declared_device_attachment(
    monkeypatch, role, interface, allowed
):
    import json
    from types import SimpleNamespace

    from nika.mcp import session_context
    from nika.mcp.servers.common import host_server
    from nika.problems.rca import inventory
    from nika.runtime.spec import NodeIdentity, NodeRole

    env = SimpleNamespace(machine_identities={"node": NodeIdentity(NodeRole(role), ())})
    monkeypatch.setattr(session_context, "get_session_meta", lambda: {})
    monkeypatch.setattr(inventory, "load_session_offline_net_env", lambda _: env)
    monkeypatch.setattr(inventory, "interfaces_for_node", lambda *_: ["eth1"])
    reads = []

    def read(node, port):
        reads.append((node, port))
        return "down"

    monkeypatch.setattr(
        host_server,
        "get_lab_runtime",
        lambda: SimpleNamespace(get_interface_operstate=read),
    )
    result = host_server.device_interface_state("node", interface)
    if allowed:
        assert json.loads(result) == {"interface": "eth1", "operstate": "down"}
        assert reads == [("node", "eth1")]
    else:
        assert result["error"] == "tool_execution_error"
        assert not reads


def test_compact_nat_read_preserves_original_and_translated_session():
    from nika.mcp.device_read import conntrack_display

    raw = "tcp 6 123 ESTABLISHED src=10.0.0.2 dst=198.51.100.10 sport=30001 dport=80 src=198.51.100.10 dst=192.0.2.1 sport=80 dport=40001 [ASSURED] mark=0 use=1\nconntrack v1: 1 flow entries have been shown."
    assert (
        conntrack_display(raw)
        == "ESTABLISHED 10.0.0.2:30001 -> 198.51.100.10:80 => 192.0.2.1:40001\nconntrack v1: 1 flow entries have been shown."
    )


def test_nat_inventory_precedes_device_and_gateway_byte_bounds(tmp_path, monkeypatch):
    import os
    import subprocess
    from nika.mcp.device_read import MAX_OUTPUT, bounded_command, conntrack_display
    from nika.mcp.tool_output import MAX_CHARS

    def row(port):
        return f"tcp 6 120 ESTABLISHED src=10.0.0.2 dst=198.51.100.10 sport=30001 dport=80 src=198.51.100.10 dst=192.0.2.1 sport=80 dport={port} [ASSURED] mark=0 use=1\n"

    # The last mappings must survive even when they arrive after 64 KiB of rows.
    raw = row(40000) * 1000 + "".join(row(p) for p in range(40001, 40064))
    assert len(raw) > MAX_OUTPUT
    table = tmp_path / "table"
    table.write_text(raw)
    executable = tmp_path / "conntrack"
    executable.write_text('#!/bin/sh\ncat "$CONNTRACK_TABLE"\n')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("CONNTRACK_TABLE", str(table))
    output = subprocess.check_output(
        bounded_command(["conntrack", "-L", "-p", "tcp"]), shell=True, text=True
    )
    assert len(output.encode()) <= MAX_OUTPUT
    expected = "REPLY_DST_PORTS 192.0.2.1 [40000-40063]"
    assert output.startswith(expected + "\n")
    rendered = conntrack_display(output)
    assert rendered.startswith(expected + "\n")
    assert expected in rendered[:MAX_CHARS]
    assert "10.0.0.2:30001 -> 198.51.100.10:80 => 192.0.2.1:40000" in rendered


@pytest.mark.parametrize("platform", ["frr", "iosxr", "routeros", "srl", "sonic"])
def test_configuration_text_remains_one_cli_argument(platform):
    import shlex
    from types import SimpleNamespace
    from nika.service.lab.frr_api import FRRAPIMixin
    from nika.service.lab.iosxr_api import IOSXRAPIMixin
    from nika.service.lab.routeros_api import RouterOSAPIMixin
    from nika.service.containerlab.srl_api import SRLAPIMixin
    from nika.service.containerlab.sonic_api import SonicAPIMixin

    calls = []
    api = SRLAPIMixin() if platform == "srl" else SimpleNamespace()
    api.exec_cmd = lambda node, command, **kwargs: calls.append(command) or "ok"
    payload = "configure terminal\ninterface lo\ndescription '$(id); `id`'\nend"
    methods = {
        "frr": FRRAPIMixin.frr_exec,
        "iosxr": IOSXRAPIMixin.iosxr_exec,
        "routeros": RouterOSAPIMixin.routeros_exec,
        "srl": SRLAPIMixin.srl_exec_cli,
        "sonic": SonicAPIMixin.sonic_exec_cli,
    }
    command = f"vtysh -c {shlex.quote(payload)}" if platform == "sonic" else payload
    assert methods[platform](api, "router", command) == "ok"
    tokens = shlex.shlex(calls[0], posix=True, punctuation_chars=True)
    tokens.whitespace_split = True
    tokens.commenters = ""
    assert (payload + "\n" if platform == "srl" else payload) in list(tokens)
