from __future__ import annotations
from pathlib import Path
from nika.runtime.containerlab.runtime import ContainerlabRuntime


class ClabConnectedDevicesTest:
    def test_min3clos_neighbors(self) -> None:
        template = (
            Path(__file__).resolve().parents[3]
            / "src/nika/net_env/min3clos/min3clos.clab.yml.tmpl"
        )
        runtime = ContainerlabRuntime(lab_name="min3clos__test", topology_file=template)

        assert runtime.get_connected_devices("leaf1") == ["client1", "spine"]

        assert runtime.get_connected_devices("client1") == ["leaf1"]
