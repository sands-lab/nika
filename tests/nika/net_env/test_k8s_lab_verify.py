from __future__ import annotations

import re
import time

import pytest

from nika.net_env.k8s_lab.lab import K8sFatTreeBGP
from nika.service.kathara.base_api import KatharaBaseAPI
from tests.support.integration_base import SharedSessionTestCase
from tests.support.net_env import instantiate_with_mocked_kathara, ready_node_count
from tests.support.prerequisites import docker_available, privileged_lab_supported


class K8sLabUnitTest:
    """Verify k8s_lab lab structure without Docker."""

    def _inst(self) -> K8sFatTreeBGP:
        return instantiate_with_mocked_kathara(
            "nika.net_env.k8s_lab.lab.Kathara.get_instance",
            K8sFatTreeBGP,
        )

    def test_has_frr_routers(self) -> None:
        """k8s_lab must expose its FRR routers through the base-class routers list."""
        inst = self._inst()

        assert len(inst.routers) > 0, "Expected at least one FRR router"
        expected_routers = {
            "leaf_1_1",
            "leaf_1_2",
            "spine_1_1",
            "spine_1_2",
            "spine_2_1",
            "spine_2_2",
            "leaf_2_1",
            "leaf_2_2",
            "core_1_1",
            "core_1_2",
            "dc_exit",
            "as1r1",
            "as2r1",
        }

        assert set(inst.routers) == expected_routers

    def test_has_kubernetes_nodes(self) -> None:
        """k8s_lab must classify k3s machines into kubernetes_nodes."""
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
        """k8s_lab must have the client node classified as a host."""
        inst = self._inst()

        assert "client" in inst.hosts

    def test_as2r1_is_bridged(self) -> None:
        """as2r1 must be bridged to provide internet connectivity."""
        inst = self._inst()

        assert inst.lab.machines["as2r1"].is_bridged()

    def test_controller_is_bridged(self) -> None:
        """controller is bridged so Docker can publish the API port to the host."""
        inst = self._inst()

        assert inst.lab.machines["controller"].is_bridged()

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
                assert args.rstrip('"').endswith("agent") or "k3s agent" in args

    def test_k3s_nodes_are_privileged(self) -> None:
        """k3s nodes must run in privileged mode."""
        inst = self._inst()
        for node_name in inst.kubernetes_nodes:
            machine = inst.lab.machines[node_name]

            assert machine.is_privileged(), (
                f"Expected {node_name} to be privileged but it is not"
            )


@pytest.mark.skipif(
    not (docker_available() and privileged_lab_supported()),
    reason="Requires Docker and root (privileged k3s containers)",
)
class K8sLabIntegrationTest(SharedSessionTestCase):
    """End-to-end checks for k8s_lab after deploy and controller.startup."""

    SCENARIO = K8sFatTreeBGP.LAB_NAME
    _READY_TIMEOUT_SEC = 900
    _api: KatharaBaseAPI

    @pytest.fixture(scope="class", autouse=True)
    def _setup_after_shared(self, _shared_session) -> None:
        """Wait for k3s/apps after SharedSessionMixin starts the lab."""
        cls = type(self)
        cls._api = KatharaBaseAPI(lab_name=cls._lab_name())
        cls._wait_until_ready()

    @classmethod
    def _lab_name(cls) -> str:
        from nika.utils.session_store import SessionStore

        return SessionStore().get_session(cls.session_id)["lab_name"]

    @classmethod
    def _exec(cls, host: str, command: str, timeout: float = 120) -> str:
        return cls._api.exec_cmd(host, command, timeout=timeout)

    @classmethod
    def _wait_until_ready(cls) -> None:
        """Poll until k3s, ingress, and sample apps are serving traffic."""
        deadline = time.time() + cls._READY_TIMEOUT_SEC
        last_error = "timeout"
        while time.time() < deadline:
            try:
                nodes = cls._exec(
                    "controller", "kubectl get nodes --no-headers", timeout=60
                )
                ready_nodes = ready_node_count(nodes)
                if ready_nodes < 6:
                    last_error = f"k3s nodes not ready ({ready_nodes}/6)"
                    time.sleep(15)
                    continue
                ingress = cls._exec(
                    "controller",
                    "kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath={.status.loadBalancer.ingress[0].ip}",
                    timeout=60,
                ).strip()
                if not ingress.startswith("101."):
                    last_error = f"ingress VIP missing (got {ingress!r})"
                    time.sleep(15)
                    continue
                code = cls._exec(
                    "client",
                    "curl -s -o /dev/null -w '%{http_code}' http://datacenter.com/word",
                    timeout=60,
                ).strip()
                if code != "200":
                    last_error = f"word app HTTP {code!r}"
                    time.sleep(15)
                    continue
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(15)
        raise TimeoutError(
            f"k8s_lab not ready within {cls._READY_TIMEOUT_SEC}s: {last_error}"
        )

    def test_bgp_spine_neighbors_up(self) -> None:
        """Pod-1 leaf routers must peer with spine routers (AS 64514)."""
        output = self._exec("leaf_1_1", "vtysh -c 'show bgp summary'")

        assert "64514" in output

        assert re.search("eth[01]\\s+4\\s+64514\\s+\\d+\\s+\\d+\\s+\\d+", output)

    def test_metallb_route_on_leaf(self) -> None:
        """MetalLB VIP must be reachable in the leaf routing table via BGP."""
        output = self._exec("leaf_1_1", "vtysh -c 'show ip route'")

        assert re.search("101\\.0\\.0\\.1/32", output)

    def test_k3s_cluster_ready(self) -> None:
        """All six k3s nodes must report Ready."""
        output = self._exec("controller", "kubectl get nodes --no-headers")

        assert ready_node_count(output) == 6, output

    def test_ingress_loadbalancer_vip(self) -> None:
        """Ingress controller must receive a MetalLB IP in 101.0.0.0/8."""
        output = self._exec(
            "controller", "kubectl get svc -n ingress-nginx ingress-nginx-controller"
        )

        assert re.search("101\\.\\d+\\.\\d+\\.\\d+", output)

    def test_cross_leaf_reachability(self) -> None:
        """Controller (leaf_1_1) must reach worker3 (leaf_1_2)."""
        output = self._exec("controller", "ping -c 3 201.2.1.2")

        assert "3 packets received" in output

    def test_client_reaches_controller(self) -> None:
        """External client must reach the k3s controller host IP."""
        output = self._exec("client", "ping -c 3 201.1.1.2")

        assert "3 received" in output

    def test_client_word_app_http(self) -> None:
        """Client must reach the word app through ingress."""
        code = self._exec(
            "client",
            "curl -s -o /dev/null -w '%{http_code}' http://datacenter.com/word",
        ).strip()

        assert code == "200"

    def test_client_weather_app_http(self) -> None:
        """Client must reach the weather app through ingress."""
        code = self._exec(
            "client",
            "curl -s -o /dev/null -w '%{http_code}' 'http://datacenter.com/weather?location=London'",
        ).strip()

        assert code == "200"
        body = self._exec(
            "client", "curl -s 'http://datacenter.com/weather?location=London'"
        )

        assert "London" in body
