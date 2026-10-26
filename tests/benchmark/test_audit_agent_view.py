"""Agent-view audit: round classification, masking, leak tokens, and snapshot diff."""

from __future__ import annotations

import re

import pytest

from experiment.audit.agent_view import (
    SNAPSHOT_MARK,
    bgp_peer_states,
    classify_rounds,
    diff_snapshots,
    fault_tokens,
    mask_line,
    parse_global_addresses,
    parse_ping_batch,
    parse_resolve_batch,
    parse_snapshot,
    summarize_agent_view,
    token_pattern,
    zone_names,
)
from experiment.audit.environment import CaseAudit, CaseIdentity, StageResult

pytestmark = pytest.mark.unit


def test_classify_rounds_separates_stable_from_flaky_and_unmeasured() -> None:
    rounds = [
        {"a": True, "b": False, "c": False, "d": None},
        {"a": True, "b": False, "c": True, "d": True},
    ]
    assert classify_rounds(rounds) == {
        "a": "holds",
        "b": "violated",
        "c": "flaky",
        "d": "error",
    }
    # A finding listed in one round only is flaky, not unmeasured.
    assert classify_rounds([{"x": False}, {}], missing=True) == {"x": "flaky"}
    assert classify_rounds([{"x": False}, {}]) == {"x": "error"}


def test_summary_fails_only_on_findings_seen_in_every_round() -> None:
    first = {
        "pingmesh": {"pingmesh unreachable a -> b": False},
        "ping4": {"ping4 a -> b": False, "ping4 b -> a": True},
        "k8s": {"k8s no Warning event: x": False},
    }
    second = {
        "pingmesh": {"pingmesh unreachable a -> b": False},
        "ping4": {"ping4 a -> b": False, "ping4 b -> a": True},
        "k8s": {},
    }
    summary = summarize_agent_view([first, second], [{}, {}])
    assert summary["violated"] == ["ping4 a -> b", "pingmesh unreachable a -> b"]
    assert summary["flaky"] == ["k8s no Warning event: x"]
    assert summary["error_count"] == 0


def test_probe_output_parsers() -> None:
    ping = "@@ 10.0.0.2 3 received\n@@ 10.0.0.3 0 packets received\n@@ 10.0.0.4 none\n"
    assert parse_ping_batch(ping, ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"]) == {
        "10.0.0.2": True,
        "10.0.0.3": False,
        "10.0.0.4": None,
        "10.0.0.5": None,
    }
    assert parse_ping_batch("@@NOTOOL", ["x"]) == {"x": None}
    dns = "@@ web.example 10.0.0.9      web.example\n@@ db.example __NO_ANSWER__\n"
    assert parse_resolve_batch(dns, ["web.example", "db.example"]) == {
        "web.example": True,
        "db.example": False,
    }
    vrfs = (
        '{"default": {"ipv4Unicast": {"peers": {"10.0.0.1": {"state": "Established"}}}},'
        ' "guest": {"ipv4Unicast": {"peers": {"10.9.0.1": {"state": "Active"}}}}}'
    )
    assert bgp_peer_states(vrfs) == {
        "default/10.0.0.1": "Established",
        "guest/10.9.0.1": "Active",
    }
    single = '{"ipv4Unicast": {"peers": {"10.0.0.1": {"state": "Idle"}}}}'
    assert bgp_peer_states(single) == {"default/10.0.0.1": "Idle"}
    assert bgp_peer_states("% BGP instance not found") == {}


def test_zone_names_reads_published_records() -> None:
    zone = (
        "$TTL 60\n@ IN SOA ns.pod0. admin.pod0. 1 1 1 1 1\n"
        "@ IN NS ns\nns IN A 10.0.0.53\nweb0 IN A 10.0.1.2\n; old IN A 1.1.1.1\n"
    )
    stock_localhost = (
        "@ IN SOA localhost. root.localhost. 1 1 1 1 1\n@ IN A 127.0.0.1\n"
    )
    campus = "web0 IN  A  10.200.0.3\n"
    files = {
        "/etc/bind/db.pod0": zone,
        "/a/etc/bind/db.local": stock_localhost,
        "/b/etc/bind/db.local": campus,
    }
    assert zone_names(files) == ["ns.pod0", "web0.local", "web0.pod0"]


def test_leak_tokens_name_the_fault_without_matching_healthy_words() -> None:
    assert fault_tokens("host_incorrect_dns") == [
        "host[-_]incorrect[-_]dns",
        "host[-_]incorrect",
        "incorrect[-_]dns",
        "hostincorrectdns",
    ]
    # Two-word ids keep separators so iproute2's ``linkdown`` flag is no token.
    assert fault_tokens("link_down") == ["link[-_]down"]
    pattern = re.compile(token_pattern("host_incorrect_dns"), re.I)
    for leak in (
        "/etc/dhcp/dhclient-enter-hooks.d/nika-incorrect-dns",
        "table internal_fault_drop",
        "class HostIncorrectDns",
        "# injected by chaos",
    ):
        assert pattern.search(leak), leak
    for healthy in (
        "default via 10.0.0.1 dev eth0",
        "nameserver 10.0.0.53",
        "/etc/resolv.conf",
    ):
        assert not pattern.search(healthy), healthy


def test_mask_line_hides_per_deployment_values_and_keeps_config() -> None:
    assert mask_line('users:(("bgpd",pid=812,fd=22))') == mask_line(
        'users:(("bgpd",pid=77,fd=3))'
    )
    assert mask_line(":INPUT ACCEPT [1203:88123]") == ":input accept [<n>:<n>]"
    assert mask_line(
        "cookie=0x0, duration=12.5s, n_packets=7, n_bytes=600, priority=10"
    ) == mask_line(
        "cookie=0x0, duration=99.1s, n_packets=70, n_bytes=6000, priority=10"
    )
    assert (
        mask_line("2026-10-18T08:01:11Z started isisd.68") == "<time> started isisd.<n>"
    )
    # Values an injector changes stay visible.
    assert mask_line("neighbor 10.0.0.23 maximum-prefix 100") != mask_line(
        "neighbor 10.0.0.23 maximum-prefix 10"
    )


def _node(files: dict[str, str], hits=None, **text: list[str]) -> dict:
    return {"files": files, "hits": hits or {}, "text": text}


def test_parse_snapshot_drops_the_snapshot_command_itself() -> None:
    raw = (
        "@@@files\n" + "a" * 32 + "  /etc/hosts\n"
        "@@@hits\n/tmp/x.conf:2\n"
        "@@@processes\nUSER COMMAND\nroot bgpd -d pid=4\n"
        f"root sh -c : {SNAPSHOT_MARK}; echo nika\n"
    )
    parsed = parse_snapshot(raw)
    assert parsed["files"] == {"/etc/hosts": "a" * 32}
    assert parsed["hits"] == {"/tmp/x.conf": 2}
    assert parsed["text"]["processes"] == ["user command", "root bgpd -d pid=<n>"]


def test_diff_flags_names_tokens_and_prefault_copies_only() -> None:
    pattern = token_pattern("host_incorrect_dns")
    before = _node(
        {
            "/etc/frr/frr.conf": "1" * 32,
            "/etc/resolv.conf": "2" * 32,
            "/etc/notes.txt": "3" * 32,
            "/var/log/syslog": "4" * 32,
            "/var/lib/nika/agent.status.json": "b" * 32,
        },
        hits={"/etc/notes.txt": 1},
        processes=["root bgpd"],
        routes=["default via 10.0.0.1 dev eth0"],
    )
    after = _node(
        {
            "/etc/frr/frr.conf": "5" * 32,
            "/etc/frr/frr.conf.keep": "1" * 32,
            "/var/tmp/frr/bgpd.41/logbuf.3": "1" * 32,
            "/etc/resolv.conf": "6" * 32,
            "/etc/notes.txt": "7" * 32,
            "/var/log/syslog": "8" * 32,
            "/tmp/nika-010-dns": "9" * 32,
            "/etc/frr/daemons.bak": "a" * 32,
            "/var/lib/nika/agent.status.json": "c" * 32,
        },
        hits={"/etc/notes.txt": 1, "/etc/resolv.conf": 1},
        processes=[
            "root bgpd",
            "root gzip -6 -c /dev/shm/archive-job/input.dat",
            "root stress-ng --cpu 2",
            "root sleep 9 # inject",
        ],
        routes=["default via 10.0.0.1 dev eth0", "10.9.0.0/24 via 10.0.0.9"],
    )
    diff = diff_snapshots(before, after, pattern)
    flagged = {
        item.get("path") or item.get("line"): item["why"] for item in diff["flagged"]
    }
    assert flagged == {
        "/etc/frr/frr.conf.keep": ["copy_of_prefault:/etc/frr/frr.conf"],
        "/etc/resolv.conf": ["content_token"],
        "/tmp/nika-010-dns": ["name_token"],
        "/etc/frr/daemons.bak": ["backup_name"],
        "root sleep 9 # inject": ["line_token"],
        "root stress-ng --cpu 2": ["line_token"],
    }
    # A ring buffer that happens to equal a pre-fault config is not a copy.
    # A changed file whose token count did not grow, a pre-existing file whose
    # name was already visible, a rotated log, an ordinary new process and a new
    # route are changes, not leaks.
    assert diff["files_changed"] == [
        "/etc/frr/frr.conf",
        "/etc/notes.txt",
        "/etc/resolv.conf",
        "/var/lib/nika/agent.status.json",
        "/var/log/syslog",
    ]
    assert diff["new_processes"] == [
        "root gzip -6 -c /dev/shm/archive-job/input.dat",
        "root sleep 9 # inject",
        "root stress-ng --cpu 2",
    ]
    assert diff["lines_added"] == {"processes": 3, "routes": 1}


def _audit(fault: str, stages: list[tuple[str, str]]) -> CaseAudit:
    return CaseAudit(
        identity=CaseIdentity(scenario="dc_clos", fault=fault),
        stages=[StageResult(stage=name, status=status) for name, status in stages],
    )


def test_agent_view_stages_are_required_for_admission() -> None:
    healthy = [("baseline_lab", "pass"), ("final_lab", "pass")]
    full = [*healthy, ("agent_view", "pass")]
    assert _audit("healthy", full).admission() == "pass"
    assert _audit("healthy", healthy).admission() == "no_evidence"
    for status in ("no_evidence", "fail"):
        stages = [*healthy, ("agent_view", status)]
        assert _audit("healthy", stages).admission() == status

    fault = [
        "baseline_lab",
        "baseline_path",
        "sham_symptom",
        "baseline_agent_view",
        "inject_artifact",
        "leak_scan",
        "symptom",
        "control_path",
        "persistence_artifact",
        "persistence_symptom",
        "final_artifact",
        "final_symptom",
    ]
    assert _audit("link_down", [(name, "pass") for name in fault]).admission() == "pass"
    without_scan = [(name, "pass") for name in fault if name != "leak_scan"]
    assert _audit("link_down", without_scan).admission() == "no_evidence"


def test_global_addresses_lead_with_eth0_not_kernel_order() -> None:
    output = (
        "5: eth1    inet 20.200.0.1/24 scope global eth1\n"
        "6: eth0    inet 10.200.0.7/24 scope global eth0\n"
        "7: eth10   inet 10.9.0.1/24 scope global eth10\n"
    )
    assert parse_global_addresses(output)[4] == ["10.200.0.7", "20.200.0.1", "10.9.0.1"]
