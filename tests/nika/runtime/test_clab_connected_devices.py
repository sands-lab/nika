from __future__ import annotations

import pytest
from nika.net_env.net_env_pool import get_net_env_instance
from nika.runtime.containerlab.runtime import ContainerlabRuntime

pytestmark = pytest.mark.unit


class ClabConnectedDevicesTest:
    def test_dc_clos_neighbors(self, tmp_path) -> None:
        env = get_net_env_instance("dc_clos", backend="containerlab", topo_size="s")
        import yaml

        template = tmp_path / "dc_clos.clab.yml"
        template.write_text(yaml.safe_dump(env._document()))
        runtime = ContainerlabRuntime(lab_name="dc_clos__test", topology_file=template)

        assert set(runtime.get_connected_devices("leaf_1")) == {
            "client_1_1",
            "client_1_2",
            "spine_1",
            "spine_2",
            "web_1",
        }

        assert runtime.get_connected_devices("client_1_1") == ["leaf_1"]
