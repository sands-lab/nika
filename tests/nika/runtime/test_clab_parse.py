from __future__ import annotations

import pytest
import tempfile
from pathlib import Path
from nika.net_env.net_env_pool import get_net_env_instance
from nika.runtime.containerlab import parse_clab_topology

pytestmark = pytest.mark.unit


class ClabParseTest:
    def test_parse_binds_and_exec(self) -> None:
        content = '\nname: demo\ntopology:\n  nodes:\n    host:\n      kind: linux\n      image: alpine:latest\n      binds:\n        - /tmp/demo:/demo\n      exec:\n        - ip link set eth1 up\n  links:\n    - endpoints: ["host:eth1", "host:eth2"]\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.clab.yml"
            path.write_text(content, encoding="utf-8")
            spec = parse_clab_topology(path)

            assert spec.nodes[0].binds == ["/tmp/demo:/demo"]

            assert spec.nodes[0].exec_cmds == ["ip link set eth1 up"]

    def test_invalid_topology_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.clab.yml"
            path.write_text("nodes: {}", encoding="utf-8")
            with pytest.raises(ValueError):
                parse_clab_topology(path)

    def test_dc_clos_env_uses_parsed_topology(self) -> None:
        env = get_net_env_instance("dc_clos", backend="containerlab")
        spec = env.get_lab_spec()

        assert spec.name == "dc_clos"

        assert len(spec.nodes) == 18

        assert "leaf_1" in env.get_info()


def test_in_memory_document_matches_yaml_roundtrip(tmp_path):
    import yaml

    data = {
        "name": "example",
        "topology": {
            "nodes": {
                "r1": {
                    "kind": "nokia_srlinux",
                    "image": "router:local",
                    "binds": ["a:b:ro"],
                },
                "pc": {"kind": "linux", "exec": ["ip route add default via 192.0.2.1"]},
            },
            "links": [{"endpoints": ["r1:e1-1", "pc:eth1"]}],
        },
    }
    path = tmp_path / "topology.clab.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    assert parse_clab_topology(data) == parse_clab_topology(path)
