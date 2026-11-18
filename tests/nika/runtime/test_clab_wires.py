"""Host-side Containerlab wires stay outside node namespaces."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.runtime.containerlab.wires import (
    delete_script,
    join_script,
    join_timeout_sec,
    join_wires,
    leftover_wire_ends,
    load_wired_pairs,
    remove_leftover_wire_ends,
    wire_end_name,
    wire_ends_for_topology,
    wire_links,
    write_wired_topology,
)

pytestmark = pytest.mark.unit

# Linux MAX_ARG_STRLEN is 32 pages (128 KiB); a single `sh -c` argument
# larger than that raises E2BIG (isp_brain has hundreds of wires).
_MAX_ARG_STRLEN = 32 * 4096


def test_wire_end_name_is_short_and_stable() -> None:
    name = wire_end_name("isp_brain__lab", "zib:eth1")
    assert name.startswith("nw")
    assert len(name) == 14
    assert name == wire_end_name("isp_brain__lab", "zib:eth1")
    assert name != wire_end_name("isp_brain__lab", "zib:eth2")


def test_wire_links_rewrites_node_pairs_and_leaves_other_links() -> None:
    host_mgmt = {"endpoints": ["leaf1:eth3", "host:mgmt0"]}
    dangling = {"endpoints": ["leaf1:eth4"]}
    document = {
        "name": "demo",
        "topology": {
            "kinds": {"linux": {"image": "alpine"}},
            "nodes": {
                "leaf1": {"kind": "linux"},
                "spine": {"kind": "linux"},
                "client": {"kind": "linux"},
            },
            "links": [
                {"endpoints": ["leaf1:eth1", "spine:eth1"], "mtu": 9100},
                {"endpoints": ["leaf1:eth2", "client:eth1"]},
                host_mgmt,
                dangling,
            ],
        },
    }
    wired, wires = wire_links(document, "demo")
    assert wired["name"] == "demo"
    assert wired["topology"]["kinds"] == document["topology"]["kinds"]
    assert wired["topology"]["nodes"] == document["topology"]["nodes"]
    assert host_mgmt in wired["topology"]["links"]
    assert dangling in wired["topology"]["links"]
    host_links = [
        link for link in wired["topology"]["links"] if link not in (host_mgmt, dangling)
    ]
    assert len(host_links) == 4 and len(wires) == 2
    assert all(
        ep.startswith("host:nw") for link in host_links for ep in link["endpoints"][1:]
    )
    mtu_links = [link for link in host_links if link.get("mtu") == 9100]
    assert len(mtu_links) == 2
    host_ends = [end for pair in wires for end in pair]
    assert len(host_ends) == len(set(host_ends))
    assert all(len(name) <= 15 for name in host_ends)
    by_node_end = {
        link["endpoints"][0]: link["endpoints"][1].split(":", 1)[1]
        for link in host_links
    }
    assert {by_node_end["leaf1:eth1"], by_node_end["spine:eth1"]} == set(wires[0])
    assert {by_node_end["leaf1:eth2"], by_node_end["client:eth1"]} == set(wires[1])


def test_join_wires_pass_large_scripts_on_stdin() -> None:
    wires = [(f"nwa{i:09d}", f"nwb{i:09d}") for i in range(500)]
    script = join_script(wires)
    assert len(script.encode()) > _MAX_ARG_STRLEN
    assert script.startswith("set -e\n")
    with patch("nika.runtime.containerlab.wires.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        join_wires(wires)
    args, kwargs = run.call_args
    assert args[0] == ["sudo", "-n", "sh"]
    assert kwargs["input"] == script
    assert "-c" not in args[0]
    assert kwargs["timeout"] == join_timeout_sec(500)


def test_write_wired_topology_round_trips_and_skips_existing_host_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "demo.clab.yml"
    path.write_text(
        "name: demo\ntopology:\n  nodes:\n    leaf1: {}\n    spine: {}\n"
        "    client: {}\n  links:\n"
        "    - endpoints: [leaf1:eth1, spine:eth1]\n"
        "    - endpoints: [leaf1:eth2, client:eth1]\n"
        "    - endpoints: [leaf1:eth3, host:mgmt0]\n",
        encoding="utf-8",
    )
    wired, wires = write_wired_topology(path, "demo")
    assert wired.name.endswith(".wired.clab.yml") and wired.is_file()
    assert load_wired_pairs(path) == wires
    assert len(wires) == 2
    assert wire_ends_for_topology(path, "demo") == [
        end for pair in wires for end in pair
    ]


def test_leftover_wire_ends_delete_only_this_lab() -> None:
    ours = ["nwaaaaaaaaaaaa", "nwbbbbbbbbbbbb"]
    output = (
        "1: lo: <LOOPBACK,UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
        "7: nwaaaaaaaaaaaa@if8: <BROADCAST,UP> mtu 1500 qdisc noqueue state UP\n"
        "9: nwcccccccccccc@if10: <BROADCAST,UP> mtu 1500 qdisc noqueue state UP\n"
    )
    assert leftover_wire_ends(ours, output) == ["nwaaaaaaaaaaaa"]
    script = delete_script(ours)
    assert "set -e" not in script
    assert script.splitlines() == [
        "ip link delete dev nwaaaaaaaaaaaa 2>/dev/null || true",
        "ip link delete dev nwbbbbbbbbbbbb 2>/dev/null || true",
    ]
    captured: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        if cmd == ["ip", "-o", "link", "show"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")
        captured["cmd"] = cmd
        captured["script"] = kwargs["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("nika.runtime.containerlab.wires.subprocess.run", side_effect=fake_run):
        removed = remove_leftover_wire_ends(ours)
    assert removed == ["nwaaaaaaaaaaaa"]
    assert captured["cmd"] == ["sudo", "-n", "sh"]
    assert captured["script"] == delete_script(["nwaaaaaaaaaaaa"])
    assert "nwcccccccccccc" not in captured["script"]
    with patch("nika.runtime.containerlab.wires.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            ["ip"], 0, stdout="1: lo: <LOOPBACK>\n", stderr=""
        )
        assert remove_leftover_wire_ends(["nwaaaaaaaaaaaa"]) == []
    assert run.call_count == 1
