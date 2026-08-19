from __future__ import annotations

from nika.net_env.kathara.kubernetes.llmd_lab.lab import LLMDInferenceCluster
from tests.support.net_env import instantiate_with_mocked_kathara


class LLMDLabUnitTest:
    """Verify llmd_lab lab structure without Docker."""

    def _inst(self) -> LLMDInferenceCluster:
        return instantiate_with_mocked_kathara(
            "nika.net_env.kathara.kubernetes.llmd_lab.lab.Kathara.get_instance",
            LLMDInferenceCluster,
        )

    def test_has_kubernetes_nodes(self) -> None:
        """llmd_lab must classify all k3s machines into kubernetes_nodes."""
        inst = self._inst()
        expected_k8s = {
            "controller",
            "worker1",
            "worker2",
            "worker3",
            "worker4",
            "worker5",
        }

        assert set(inst.kubernetes_nodes) == expected_k8s

    def test_has_client_host(self) -> None:
        """llmd_lab must have the client node classified as a host."""
        inst = self._inst()

        assert "client" in inst.hosts

    def test_all_k3s_nodes_are_bridged(self) -> None:
        """All k3s nodes must have bridged=True for internet access."""
        inst = self._inst()
        for node_name in inst.kubernetes_nodes:
            machine = inst.lab.machines[node_name]

            assert machine.is_bridged(), (
                f"Expected {node_name} to be bridged but it is not"
            )

    def test_k3s_nodes_defer_k3s_until_net_ready(self) -> None:
        """Entrypoint waits for startup net-ready flag, then execs k3s as PID1."""
        inst = self._inst()
        for node_name in inst.kubernetes_nodes:
            machine = inst.lab.machines[node_name]
            args = str(machine.meta.get("args") or "")
            assert machine.meta.get("entrypoint") == "/bin/sh"
            assert "/var/run/nika-net-ready" in args
            assert "exec /bin/k3s" in args
            assert machine.get_image() == "rancher/k3s:v1.34.1-k3s1"
            if node_name == "controller":
                assert "server --disable" in args
            else:
                assert "k3s agent" in args

    def test_k3s_nodes_are_privileged(self) -> None:
        """k3s nodes must run in privileged mode."""
        inst = self._inst()
        for node_name in inst.kubernetes_nodes:
            machine = inst.lab.machines[node_name]

            assert machine.is_privileged(), (
                f"Expected {node_name} to be privileged but it is not"
            )
