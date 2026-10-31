"""Host-side Containerlab wires stay outside node namespaces."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nika.runtime.containerlab.wires import (
    join_script,
    join_timeout_sec,
    join_wires,
    wire_end_name,
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


def test_wire_links_rewrites_node_to_node_pairs() -> None:
    document = {
        "topology": {
            "nodes": {"a": {}, "b": {}, "pc_a": {}},
            "links": [
                {"endpoints": ["a:eth1", "b:eth1"]},
                {"endpoints": ["a:eth2", "pc_a:eth1"]},
            ],
        }
    }
    wired, wires = wire_links(document, "lab")
    ends = [link["endpoints"] for link in wired["topology"]["links"]]
    assert len(wires) == 2
    assert all(ep.startswith("host:nw") for pair in ends for ep in pair[1:])
    assert {ep.split(":", 1)[0] for pair in ends for ep in pair[:1]} == {
        "a",
        "b",
        "pc_a",
    }


def test_join_script_for_large_labs_exceeds_argv_limit() -> None:
    wires = [(f"nwa{i:09d}", f"nwb{i:09d}") for i in range(500)]
    script = join_script(wires)
    assert len(script.encode()) > _MAX_ARG_STRLEN
    assert script.startswith("set -e\n")
    assert "redirect dev nwb000000000" in script


def test_join_wires_passes_script_on_stdin() -> None:
    wires = [(f"nwa{i:09d}", f"nwb{i:09d}") for i in range(500)]
    script = join_script(wires)

    with patch("nika.runtime.containerlab.wires.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        join_wires(wires)

    args, kwargs = run.call_args
    assert args[0] == ["sudo", "-n", "sh"]
    assert kwargs["input"] == script
    assert "-c" not in args[0]
    assert kwargs["timeout"] == join_timeout_sec(500)


def test_write_wired_topology(tmp_path: Path) -> None:
    path = tmp_path / "isp.clab.yml"
    path.write_text(
        "name: lab\ntopology:\n  nodes:\n    a: {}\n    b: {}\n"
        "  links:\n    - endpoints: [a:eth1, b:eth1]\n",
        encoding="utf-8",
    )
    wired, wires = write_wired_topology(path, "lab")
    assert wired.name.endswith(".wired.clab.yml")
    assert len(wires) == 1
    assert wired.is_file()
