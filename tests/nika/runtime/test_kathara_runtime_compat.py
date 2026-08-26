from __future__ import annotations
from nika.runtime.factory import runtime_for_net_env
from nika.runtime.kathara import KatharaRuntime
from tests.support.simple_bgp.lab import SimpleBGP


class KatharaRuntimeCompatTest:
    def test_runtime_for_kathara_net_env(self) -> None:
        env = SimpleBGP()
        runtime = runtime_for_net_env(env)

        assert isinstance(runtime, KatharaRuntime)

        assert runtime.lab_name == env.name

    def test_kathara_runtime_list_nodes_before_deploy(self) -> None:
        env = SimpleBGP()
        runtime = KatharaRuntime(env)
        nodes = runtime.list_nodes()

        assert "pc1" in nodes

        assert "router1" in nodes
