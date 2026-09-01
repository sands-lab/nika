from __future__ import annotations

import pytest
import tempfile
from pathlib import Path
from nika.net_env.net_env_pool import get_net_env_instance
from nika.runtime.containerlab import parse_clab_topology


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

    def test_min3clos_template_parses(self) -> None:
        template = (
            Path(__file__).resolve().parents[3]
            / "src/nika/net_env/min3clos/min3clos.clab.yml.tmpl"
        )
        spec = parse_clab_topology(template)

        assert spec.name == "__LAB_NAME__"
        node_names = [node.name for node in spec.nodes]

        assert len(node_names) == 5

        assert "leaf1" in node_names

        assert "spine" in node_names

        assert "client2" in node_names

        assert len(spec.links) == 4

    def test_min3clos_env_uses_parsed_topology(self) -> None:
        env = get_net_env_instance("min3clos", backend="containerlab")
        spec = env.get_lab_spec()

        assert spec.name == "min3clos"

        assert len(spec.nodes) == 5

        assert "leaf1" in env.get_info()
