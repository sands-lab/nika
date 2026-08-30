"""Live host-side Kubernetes MCP + gateway tests (k8s_lab / llmd_lab)."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import socket
import time
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig, SESSION_HEADER
from nika.service.k8s_mcp_server.client import reset_client
from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session
from nika.service.mcp_server.registry import K8S_MCP_SERVER
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session
from tests.agent._assertions import _extract_tool_names
from tests.agent.sandbox_support import (
    sandbox_anthropic_credential_available,
)
from tests.support.integration_base import SharedSessionTestCase
from tests.support.integration_pipeline import (
    claude_cli_available,
    load_test_env,
    tool_text_list,
)
from tests.support.prerequisites import docker_available, privileged_lab_supported
from nika.utils.session_id import resolve_session_tag

load_test_env()

CLAUDE_MODEL = "deepseek-v4-flash"


def _tool_payload(result: object) -> object:
    texts = tool_text_list(result)
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def _require_live_k8s() -> bool:
    return docker_available() and privileged_lab_supported()


def _session_k8s_meta(session_id: str) -> tuple[int, Path]:
    row = SessionStore().get_session(session_id)
    metadata = row.get("metadata") or {}
    port = metadata.get("k8s_controller_port")
    path_raw = metadata.get("kubeconfig_path")
    if path_raw is None:
        workdir = row.get("runtime_workdir") or (row.get("scenario_params") or {}).get(
            "runtime_workdir"
        )
        if workdir:
            path_raw = str(Path(workdir) / "kubeconfig.yaml")
    if port is None or not path_raw:
        raise AssertionError(
            f"session {session_id} missing k8s port/kubeconfig: {metadata}"
        )
    path = Path(path_raw)
    assert path.is_file(), f"missing kubeconfig {path}"
    text = path.read_text(encoding="utf-8")
    assert f"localhost:{port}" in text, f"kubeconfig server mismatch for port {port}"
    return int(port), path


def _wait_host_api(session_id: str, *, timeout_sec: float = 300.0) -> None:
    """Wait until the published API port accepts TCP and kubeconfig exists."""
    deadline = time.time() + timeout_sec
    last = ""
    while time.time() < deadline:
        try:
            port, kube = _session_k8s_meta(session_id)
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                pass
            # Also require a successful API handshake when possible.
            from kubernetes import client, config

            configuration = client.Configuration()
            config.load_kube_config(
                config_file=str(kube), client_configuration=configuration
            )
            configuration.verify_ssl = False
            api = client.CoreV1Api(client.ApiClient(configuration))
            api.list_node(_request_timeout=5)
            return
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(5)
    raise TimeoutError(f"host k8s API not ready for {session_id}: {last}")


async def _with_k8s_tools(session_id: str, scenario: str, coro_fn):
    reset_client(session_id)
    with mcp_gateway_for_session(session_id, scenario_name=scenario):
        cfg = MCPServerConfig(session_id=session_id).load_http_config([K8S_MCP_SERVER])
        assert K8S_MCP_SERVER in cfg
        client = MultiServerMCPClient(connections=cfg)
        tools = {t.name: t for t in await client.get_tools()}
        assert "k8s_list_nodes" in tools
        return await coro_fn(tools)


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
class K8sMcpGatewayIntegrationTest(SharedSessionTestCase):
    """Deploy k8s_lab once; exercise host-side MCP and k8s faults."""

    SCENARIO = "k8s_lab"
    _READY_TIMEOUT_SEC = 900

    @pytest.fixture(scope="class", autouse=True)
    def _wait_for_host_api(self, _shared_session) -> None:
        assert self.session_id is not None
        _wait_host_api(self.session_id, timeout_sec=self._READY_TIMEOUT_SEC)
        port, kube = _session_k8s_meta(self.session_id)
        assert port > 0
        assert kube.is_file()

    def test_01_gateway_lists_nodes_and_services(self) -> None:
        async def _check(tools):
            nodes = _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))
            services = _tool_payload(
                await tools["k8s_list_services"].ainvoke({"all_namespaces": True})
            )
            return nodes, services

        nodes, services = asyncio.run(
            _with_k8s_tools(self.session_id, self.SCENARIO, _check)
        )
        assert isinstance(nodes, list) and len(nodes) >= 6
        assert any(n.get("ready") for n in nodes if isinstance(n, dict))
        assert isinstance(services, list)
        assert any(
            isinstance(s, dict) and s.get("name") == "kubernetes" for s in services
        )

    def test_02_missing_session_header_rejected(self) -> None:
        import urllib.error
        import urllib.request

        assert self.session_id is not None
        with mcp_gateway_for_session(
            self.session_id, scenario_name=self.SCENARIO
        ) as gw:
            url = f"{gw.base_url}/mcp/{K8S_MCP_SERVER}/mcp"
            req = urllib.request.Request(url, method="GET")
            try:
                urllib.request.urlopen(req, timeout=5)
                pytest.fail("expected missing-session rejection")
            except urllib.error.HTTPError as exc:
                assert exc.code in {400, 401, 403, 406}

            bad = urllib.request.Request(
                url,
                method="GET",
                headers={SESSION_HEADER: "not-a-real-session"},
            )
            try:
                urllib.request.urlopen(bad, timeout=5)
            except urllib.error.HTTPError:
                pass

    def test_03_worker_apiserver_partition_symptom(self) -> None:
        assert self.session_id is not None
        inject_failure(
            ["k8s_worker_apiserver_partition"],
            session_id=self.session_id,
            param_overrides={"node_name": "worker1"},
        )
        self._assert_failure_injected("k8s_worker_apiserver_partition")

        async def _check(tools):
            deadline = time.time() + 240
            last = None
            while time.time() < deadline:
                last = _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))
                if isinstance(last, list):
                    not_ready = [
                        n for n in last if isinstance(n, dict) and not n.get("ready")
                    ]
                    if not_ready:
                        return last, not_ready
                await asyncio.sleep(10)
            return last, []

        nodes, not_ready = asyncio.run(
            _with_k8s_tools(self.session_id, self.SCENARIO, _check)
        )
        assert not_ready, f"expected a NotReady node after partition; nodes={nodes}"

        from nika.service.kathara.base_api import KatharaBaseAPI

        lab_name = SessionStore().get_session(self.session_id)["lab_name"]
        KatharaBaseAPI(lab_name=lab_name).exec_cmd(
            "worker1",
            "iptables -F OUTPUT 2>/dev/null; iptables -F INPUT 2>/dev/null; true",
            timeout=30,
        )

    def test_04_coredns_isolated_symptom(self) -> None:
        assert self.session_id is not None
        inject_failure(
            ["k8s_coredns_isolated"],
            session_id=self.session_id,
        )
        self._assert_failure_injected("k8s_coredns_isolated")

        async def _check(tools):
            endpoints = _tool_payload(
                await tools["k8s_get_endpoints"].ainvoke(
                    {"service": "kube-dns", "namespace": "kube-system"}
                )
            )
            pods = _tool_payload(
                await tools["k8s_list_pods"].ainvoke(
                    {
                        "namespace": "kube-system",
                        "selector": "k8s-app=kube-dns",
                    }
                )
            )
            app_pods = _tool_payload(
                await tools["k8s_list_pods"].ainvoke(
                    {"namespace": "word-ns", "selector": "app=word"}
                )
            )
            dns_result = None
            if isinstance(app_pods, list) and app_pods:
                pod = app_pods[0]
                dns_result = _tool_payload(
                    await tools["k8s_dns_query"].ainvoke(
                        {
                            "pod": pod["name"],
                            "namespace": pod["namespace"],
                            "query": "kubernetes.default.svc.cluster.local",
                        }
                    )
                )
            return endpoints, pods, dns_result

        endpoints, pods, dns_result = asyncio.run(
            _with_k8s_tools(self.session_id, self.SCENARIO, _check)
        )
        assert isinstance(endpoints, dict)
        assert endpoints.get("cluster_ip")
        assert isinstance(pods, list) and pods
        assert all(p.get("ready") for p in pods if isinstance(p, dict))
        if isinstance(dns_result, dict):
            stdout = (dns_result.get("stdout") or "").lower()
            stderr = (dns_result.get("stderr") or "").lower()
            ok = dns_result.get("ok")
            assert (
                ok is False
                or "nxdomain" in stdout
                or "timed out" in (stdout + stderr)
                or "servfail" in (stdout + stderr)
                or not stdout.strip()
            )

        lab_name = SessionStore().get_session(self.session_id)["lab_name"]
        from nika.service.kathara.base_api import KatharaBaseAPI

        api = KatharaBaseAPI(lab_name=lab_name)
        for node in (
            "controller",
            "worker1",
            "worker2",
            "worker3",
            "worker4",
            "worker5",
        ):
            api.exec_cmd(
                node,
                "iptables -F OUTPUT 2>/dev/null; iptables -F INPUT 2>/dev/null; true",
                timeout=30,
            )

    def test_05_clusterip_routing_broken_symptom(self) -> None:
        assert self.session_id is not None
        inject_failure(
            ["k8s_clusterip_routing_broken"],
            session_id=self.session_id,
            param_overrides={"node_name": "worker2"},
        )
        self._assert_failure_injected("k8s_clusterip_routing_broken")

        async def _check(tools):
            nodes = _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))
            endpoints = _tool_payload(
                await tools["k8s_get_endpoints"].ainvoke(
                    {"service": "kubernetes", "namespace": "default"}
                )
            )
            return nodes, endpoints

        nodes, endpoints = asyncio.run(
            _with_k8s_tools(self.session_id, self.SCENARIO, _check)
        )
        assert isinstance(nodes, list)
        ready = [n for n in nodes if isinstance(n, dict) and n.get("ready")]
        assert len(ready) >= 5
        assert isinstance(endpoints, dict)
        assert endpoints.get("cluster_ip") or endpoints.get("addresses")

        lab_name = SessionStore().get_session(self.session_id)["lab_name"]
        from nika.service.kathara.base_api import KatharaBaseAPI

        KatharaBaseAPI(lab_name=lab_name).exec_cmd(
            "worker2",
            "iptables -F OUTPUT 2>/dev/null; iptables -F INPUT 2>/dev/null; true",
            timeout=30,
        )

    def test_06_networkpolicy_deny(self) -> None:
        assert self.session_id is not None
        inject_failure(
            ["k8s_networkpolicy_deny"],
            session_id=self.session_id,
            param_overrides={
                "control_node": "controller",
                "symptom_host": "client",
                "namespace": "word-ns",
                "pod_selector": "app=word",
                "symptom_url": "http://datacenter.com/word",
                "control_url": "http://datacenter.com/weather",
            },
        )
        self._assert_failure_injected("k8s_networkpolicy_deny")
        lab_name = SessionStore().get_session(self.session_id)["lab_name"]
        from nika.service.kathara.base_api import KatharaBaseAPI

        KatharaBaseAPI(lab_name=lab_name).exec_cmd(
            "controller",
            "kubectl delete networkpolicy nika-deny-ingress -n word-ns --ignore-not-found",
            timeout=60,
        )


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
@pytest.mark.skipif(
    not (claude_cli_available() and sandbox_anthropic_credential_available()),
    reason="Claude CLI + DeepSeek/Anthropic credentials required",
)
class K8sMcpClaudeAgentE2ETest(SharedSessionTestCase):
    """cli.claude against k8s_lab with a real k8s fault."""

    SCENARIO = "k8s_lab"
    INJECT_PROBLEM = "k8s_coredns_isolated"
    INJECT_PARAMS: dict[str, str] | None = None
    _READY_TIMEOUT_SEC = 900

    @pytest.fixture(scope="class", autouse=True)
    def _wait_for_host_api(self, _shared_session) -> None:
        assert self.session_id is not None
        _wait_host_api(self.session_id, timeout_sec=self._READY_TIMEOUT_SEC)

    def test_agent_uses_k8s_mcp_tools(self) -> None:
        from agent.protocols import DIAGNOSIS, SUBMISSION
        from nika.utils.session import Session

        assert self.session_id is not None
        row = SessionStore().get_session(self.session_id)
        type(self).session_dir = Path(row["session_dir"])

        gt_path = type(self).session_dir / "ground_truth.json"
        symptom = ""
        if gt_path.is_file():
            symptom = str(
                json.loads(gt_path.read_text(encoding="utf-8")).get("detailed_cause")
                or ""
            )
        steered = (
            f"{row.get('task_description') or ''}\n\n"
            "Reported symptom (investigate with cluster tools first):\n"
            f"{symptom}\n\n"
            "IMPORTANT: Prefer the Kubernetes MCP tools from k8s_mcp_server "
            "(k8s_list_nodes, k8s_list_pods, k8s_list_services, k8s_dns_query, "
            "k8s_get_network_policies, k8s_check_connectivity, etc.) before using "
            "host/shell or FRR tools. This is primarily a Kubernetes cluster fault."
        )
        session = Session()
        session.load_running_session(session_id=self.session_id)
        session.update_session("task_description", steered)

        self._run_agent(
            agent_type="cli.claude",
            model=CLAUDE_MODEL,
            max_steps=25,
        )

        messages = [
            json.loads(line)
            for line in (type(self).session_dir / "messages.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert messages
        mcp_cfgs = [e for e in messages if e.get("event") == "mcp_config"]
        assert any(
            K8S_MCP_SERVER in str(e.get("servers") or e.get("mcp_servers") or e)
            for e in mcp_cfgs
        ) or any(K8S_MCP_SERVER in json.dumps(e) for e in mcp_cfgs)

        diag_mcp = next(
            (e for e in mcp_cfgs if e.get("agent") == DIAGNOSIS),
            None,
        )
        sub_mcp = next(
            (e for e in mcp_cfgs if e.get("agent") == SUBMISSION),
            None,
        )
        assert diag_mcp is not None
        assert K8S_MCP_SERVER in (diag_mcp.get("servers") or [])
        assert sub_mcp is not None
        assert "task_mcp_server" in (sub_mcp.get("servers") or [])

        tool_names: list[str] = []
        for entry in messages:
            tool_names.extend(_extract_tool_names(entry))
        k8s_tools = [n for n in tool_names if "k8s_" in n]
        assert k8s_tools, f"expected k8s_* MCP tool use, got {tool_names}"


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
class LlmdMcpGatewayIntegrationTest(SharedSessionTestCase):
    """Deploy llmd_lab; host MCP + one real k8s failure."""

    SCENARIO = "llmd_lab"
    _READY_TIMEOUT_SEC = 1800

    @pytest.fixture(scope="class", autouse=True)
    def _wait_for_host_api(self, _shared_session) -> None:
        assert self.session_id is not None
        _wait_host_api(self.session_id, timeout_sec=self._READY_TIMEOUT_SEC)
        _session_k8s_meta(self.session_id)

    def test_01_gateway_lists_nodes(self) -> None:
        async def _check(tools):
            return _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))

        nodes = asyncio.run(_with_k8s_tools(self.session_id, self.SCENARIO, _check))
        assert isinstance(nodes, list) and len(nodes) >= 6

    def test_02_coredns_isolated_via_mcp(self) -> None:
        assert self.session_id is not None
        inject_failure(
            ["k8s_coredns_isolated"],
            session_id=self.session_id,
        )
        self._assert_failure_injected("k8s_coredns_isolated")

        async def _check(tools):
            endpoints = _tool_payload(
                await tools["k8s_get_endpoints"].ainvoke(
                    {"service": "kube-dns", "namespace": "kube-system"}
                )
            )
            pods = _tool_payload(
                await tools["k8s_list_pods"].ainvoke(
                    {
                        "namespace": "kube-system",
                        "selector": "k8s-app=kube-dns",
                    }
                )
            )
            return endpoints, pods

        endpoints, pods = asyncio.run(
            _with_k8s_tools(self.session_id, self.SCENARIO, _check)
        )
        assert isinstance(endpoints, dict)
        assert endpoints.get("cluster_ip")
        assert isinstance(pods, list) and pods

        from nika.service.kathara.base_api import KatharaBaseAPI

        lab_name = SessionStore().get_session(self.session_id)["lab_name"]
        api = KatharaBaseAPI(lab_name=lab_name)
        for node in (
            "controller",
            "worker1",
            "worker2",
            "worker3",
            "worker4",
            "worker5",
        ):
            api.exec_cmd(
                node,
                "iptables -F OUTPUT 2>/dev/null; iptables -F INPUT 2>/dev/null; true",
                timeout=30,
            )


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
@pytest.mark.skipif(
    not (claude_cli_available() and sandbox_anthropic_credential_available()),
    reason="Claude CLI + DeepSeek/Anthropic credentials required",
)
class LlmdMcpClaudeAgentE2ETest(SharedSessionTestCase):
    """cli.claude against llmd_lab with a real k8s fault."""

    SCENARIO = "llmd_lab"
    INJECT_PROBLEM = "k8s_coredns_isolated"
    INJECT_PARAMS: dict[str, str] | None = None
    _READY_TIMEOUT_SEC = 1800

    @pytest.fixture(scope="class", autouse=True)
    def _wait_for_host_api(self, _shared_session) -> None:
        assert self.session_id is not None
        _wait_host_api(self.session_id, timeout_sec=self._READY_TIMEOUT_SEC)

    def test_agent_uses_k8s_mcp_tools(self) -> None:
        from agent.protocols import DIAGNOSIS
        from nika.utils.session import Session

        assert self.session_id is not None
        row = SessionStore().get_session(self.session_id)
        type(self).session_dir = Path(row["session_dir"])

        steered = (
            f"{row.get('task_description') or ''}\n\n"
            "IMPORTANT: Prefer Kubernetes MCP tools from k8s_mcp_server "
            "(k8s_list_nodes, k8s_list_pods, k8s_list_events) to diagnose the fault."
        )
        session = Session()
        session.load_running_session(session_id=self.session_id)
        session.update_session("task_description", steered)

        self._run_agent(
            agent_type="cli.claude",
            model=CLAUDE_MODEL,
            max_steps=25,
        )

        messages = [
            json.loads(line)
            for line in (type(self).session_dir / "messages.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        mcp_cfgs = [e for e in messages if e.get("event") == "mcp_config"]
        diag_mcp = next((e for e in mcp_cfgs if e.get("agent") == DIAGNOSIS), None)
        assert diag_mcp is not None
        assert K8S_MCP_SERVER in (diag_mcp.get("servers") or [])
        tool_names: list[str] = []
        for entry in messages:
            tool_names.extend(_extract_tool_names(entry))
        assert any("k8s_" in n for n in tool_names), tool_names


def _prod_shape_mcp_probe(
    session_id: str,
    *,
    smoke_extra_tools: bool = False,
) -> dict[str, object]:
    """Child-process probe matching production: one gateway per session.

    Parallel agent/sandbox runs use separate processes so each
    ``mcp_gateway_for_session`` owns an ephemeral listen port and does not
    share the in-process FastMCP / ``NIKA_MCP_GATEWAY_URL`` singleton.
    """
    import urllib.error
    import urllib.request

    from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session

    reset_client(session_id)
    with mcp_gateway_for_session(
        session_id,
        scenario_name="k8s_lab",
        backend="kathara",
    ) as gw:

        async def _exercise() -> dict[str, object]:
            cfg = MCPServerConfig(session_id=session_id).load_http_config(
                [K8S_MCP_SERVER]
            )
            client = MultiServerMCPClient(connections=cfg)
            tools = {t.name: t for t in await client.get_tools()}
            assert "k8s_list_nodes" in tools and "k8s_get_node" in tools

            nodes = _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))
            assert isinstance(nodes, list) and len(nodes) >= 6

            ctrl = _tool_payload(
                await tools["k8s_get_node"].ainvoke({"name": "controller"})
            )
            assert isinstance(ctrl, dict)
            uid = (ctrl.get("metadata") or {}).get("uid")
            assert uid

            if smoke_extra_tools:
                events = _tool_payload(
                    await tools["k8s_list_events"].ainvoke(
                        {"namespace": "kube-system", "limit": 20}
                    )
                )
                assert isinstance(events, list)

                netpols = _tool_payload(
                    await tools["k8s_get_network_policies"].ainvoke(
                        {"all_namespaces": True}
                    )
                )
                assert isinstance(netpols, list)

                dns_pods = _tool_payload(
                    await tools["k8s_list_pods"].ainvoke(
                        {
                            "namespace": "kube-system",
                            "selector": "k8s-app=kube-dns",
                        }
                    )
                )
                assert isinstance(dns_pods, list) and dns_pods
                pod0 = dns_pods[0]
                logs = _tool_payload(
                    await tools["k8s_get_logs"].ainvoke(
                        {
                            "name": pod0["name"],
                            "namespace": pod0["namespace"],
                            "tail_lines": 20,
                        }
                    )
                )
                assert isinstance(logs, (str, dict))

                app_pods = _tool_payload(
                    await tools["k8s_list_pods"].ainvoke(
                        {"namespace": "word-ns", "selector": "app=word"}
                    )
                )
                if isinstance(app_pods, list) and app_pods:
                    src = app_pods[0]
                    conn = _tool_payload(
                        await tools["k8s_check_connectivity"].ainvoke(
                            {
                                "pod": src["name"],
                                "namespace": src["namespace"],
                                "target": "kubernetes.default.svc.cluster.local",
                                "port": 443,
                            }
                        )
                    )
                    assert isinstance(conn, dict)

            return {
                "gateway_port": gw.port,
                "controller_uid": uid,
                "node_count": len(nodes),
            }

        result = asyncio.run(_exercise())

        # This gateway only registered *session_id* — foreign header rejected.
        url = f"{gw.base_url}/mcp/{K8S_MCP_SERVER}/mcp"
        bad = urllib.request.Request(
            url,
            method="GET",
            headers={SESSION_HEADER: "not-a-real-session"},
        )
        try:
            urllib.request.urlopen(bad, timeout=5)
            raise AssertionError("expected rejection for unknown session")
        except urllib.error.HTTPError as exc:
            assert exc.code in {400, 401, 403, 404, 406}

        return result


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
class K8sMcpParallelSessionTest:
    """Two live k8s_lab sessions with production-shaped MCP isolation.

    Production (agent run / sandbox batch / remote attach) starts one ephemeral
    MCP gateway per session in a dedicated process. This test keeps both labs
    up, then probes each session from a spawn child that calls
    ``mcp_gateway_for_session`` exactly once.

    Requires ``fs.inotify.max_user_instances`` >= ~512–1024 on the host.
    """

    def test_concurrent_labs_ports_and_mcp_isolation(self) -> None:
        from concurrent.futures import ProcessPoolExecutor

        sid_a = start_net_env(
            "k8s_lab",
            None,
            session_tag=resolve_session_tag(context="test"),
            instance_tag="par-a",
        )
        sid_b = None
        try:
            _wait_host_api(sid_a, timeout_sec=900)
            port_a, kube_a = _session_k8s_meta(sid_a)
            assert f"localhost:{port_a}" in kube_a.read_text(encoding="utf-8")

            sid_b = start_net_env(
                "k8s_lab",
                None,
                session_tag=resolve_session_tag(context="test"),
                instance_tag="par-b",
            )
            _wait_host_api(sid_b, timeout_sec=900)
            port_b, kube_b = _session_k8s_meta(sid_b)

            assert port_a != port_b, f"k8s API ports collided: {port_a}"
            assert kube_a.resolve() != kube_b.resolve()
            assert f"localhost:{port_b}" in kube_b.read_text(encoding="utf-8")
            with socket.create_connection(("127.0.0.1", port_a), timeout=3):
                pass
            with socket.create_connection(("127.0.0.1", port_b), timeout=3):
                pass

            # One gateway per session in separate processes (prod shape).
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
                fut_a = pool.submit(
                    _prod_shape_mcp_probe, sid_a, smoke_extra_tools=True
                )
                fut_b = pool.submit(
                    _prod_shape_mcp_probe, sid_b, smoke_extra_tools=False
                )
                res_a = fut_a.result(timeout=300)
                res_b = fut_b.result(timeout=300)

            assert res_a["gateway_port"] != res_b["gateway_port"], (
                f"MCP gateway ports collided: {res_a['gateway_port']}"
            )
            assert res_a["gateway_port"] not in {port_a, port_b}
            assert res_b["gateway_port"] not in {port_a, port_b}
            assert res_a["controller_uid"] != res_b["controller_uid"], (
                "MCP sessions cross-wired to the same cluster"
            )
            assert int(res_a["node_count"]) >= 6
            assert int(res_b["node_count"]) >= 6
        finally:
            for sid in (sid_a, sid_b):
                if not sid:
                    continue
                try:
                    close_session(session_id=sid)
                except Exception:  # noqa: BLE001
                    pass
            reset_client()
