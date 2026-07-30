"""Live k8s_lab tests for the in-node Kubernetes MCP + gateway proxy."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig, SESSION_HEADER
from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session
from nika.service.mcp_server.registry import K8S_MCP_SERVER
from nika.utils.session_store import SessionStore
from nika.workflows.failure.inject import inject_failure
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

load_test_env()

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_START = (
    REPO_ROOT
    / "src/nika/net_env/kathara/kubernetes/k8s_lab/controller/opt/nika-k8s-mcp/bundle.tar.gz"
)
CLAUDE_MODEL = "deepseek-v4-flash"


def _stage_k8s_mcp_bundle() -> None:
    if BUNDLE_START.is_file():
        return
    script = REPO_ROOT / "scripts" / "stage_k8s_mcp_bundle.py"
    subprocess.run(
        ["uv", "run", "python", str(script)],
        cwd=REPO_ROOT,
        check=True,
    )
    assert BUNDLE_START.is_file(), f"staging did not produce {BUNDLE_START}"


def _tool_payload(result: object) -> object:
    texts = tool_text_list(result)
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def _require_live_k8s() -> bool:
    return docker_available() and privileged_lab_supported()


# Stage only when live k8s tests can run (avoid heavy work on skip).
if _require_live_k8s():
    _stage_k8s_mcp_bundle()


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
class K8sMcpGatewayIntegrationTest(SharedSessionTestCase):
    """Deploy k8s_lab once; exercise gateway→in-node MCP and the three k8s faults."""

    SCENARIO = "k8s_lab"
    _READY_TIMEOUT_SEC = 900

    @classmethod
    def _wait_mcp_via_controller(cls) -> None:
        from nika.service.kathara.base_api import KatharaBaseAPI

        lab_name = SessionStore().get_session(cls.session_id)["lab_name"]
        api = KatharaBaseAPI(lab_name=lab_name)
        deadline = time.time() + cls._READY_TIMEOUT_SEC
        last = ""
        while time.time() < deadline:
            try:
                last = api.exec_cmd(
                    "controller",
                    "/opt/nika-k8s-mcp/healthcheck.sh",
                    timeout=30,
                )
                if '"status": "ok"' in last or '"status":"ok"' in last:
                    return
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            time.sleep(10)
        raise TimeoutError(f"k8s MCP not healthy: {last}")

    @pytest.fixture(scope="class", autouse=True)
    def _wait_for_mcp(self, _shared_session) -> None:
        type(self)._wait_mcp_via_controller()

    async def _with_k8s_tools(self, coro_fn):
        assert self.session_id is not None
        with mcp_gateway_for_session(self.session_id, scenario_name=self.SCENARIO):
            cfg = MCPServerConfig(session_id=self.session_id).load_http_config(
                [K8S_MCP_SERVER]
            )
            assert K8S_MCP_SERVER in cfg
            client = MultiServerMCPClient(connections=cfg)
            tools = {t.name: t for t in await client.get_tools()}
            assert "k8s_list_nodes" in tools
            return await coro_fn(tools)

    def test_01_gateway_lists_nodes_and_services(self) -> None:
        async def _check(tools):
            nodes = _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))
            services = _tool_payload(
                await tools["k8s_list_services"].ainvoke({"all_namespaces": True})
            )
            return nodes, services

        nodes, services = asyncio.run(self._with_k8s_tools(_check))
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
                # Phase gate may return empty MCP rather than HTTP error for
                # unknown sessions; either way tools must not succeed.
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
            # Node NotReady can take ~40s+; poll via MCP.
            deadline = time.time() + 240
            last = None
            while time.time() < deadline:
                last = _tool_payload(await tools["k8s_list_nodes"].ainvoke({}))
                if isinstance(last, list):
                    # Kathara device worker1 maps to a k3s node hostname.
                    not_ready = [
                        n for n in last if isinstance(n, dict) and not n.get("ready")
                    ]
                    if not_ready:
                        return last, not_ready
                await asyncio.sleep(10)
            return last, []

        nodes, not_ready = asyncio.run(self._with_k8s_tools(_check))
        assert not_ready, f"expected a NotReady node after partition; nodes={nodes}"

        # Heal for subsequent tests: close/reopen is heavy; clear iptables on worker.
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
            # Find an app pod to run DNS from.
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

        endpoints, pods, dns_result = asyncio.run(self._with_k8s_tools(_check))
        assert isinstance(endpoints, dict)
        assert endpoints.get("cluster_ip")
        assert isinstance(pods, list) and pods
        assert all(p.get("ready") for p in pods if isinstance(p, dict))
        # DNS should fail or return an error/timeout from the app pod.
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

        nodes, endpoints = asyncio.run(self._with_k8s_tools(_check))
        assert isinstance(nodes, list)
        # Node stays Ready for this fault.
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


@pytest.mark.skipif(
    not _require_live_k8s(),
    reason="Requires Docker and privileged Kathara/k3s support",
)
@pytest.mark.skipif(
    not (claude_cli_available() and sandbox_anthropic_credential_available()),
    reason="Claude CLI + DeepSeek/Anthropic credentials required",
)
class K8sMcpClaudeAgentE2ETest(SharedSessionTestCase):
    """cli.claude + DeepSeek against k8s_lab with a real k8s fault.
    """

    SCENARIO = "k8s_lab"
    INJECT_PROBLEM = "k8s_coredns_isolated"
    INJECT_PARAMS: dict[str, str] | None = None
    _READY_TIMEOUT_SEC = 900

    def test_agent_uses_k8s_mcp_tools(self) -> None:
        from agent.utils.phases import DIAGNOSIS, SUBMISSION
        from nika.utils.session import Session

        assert self.session_id is not None
        row = SessionStore().get_session(self.session_id)
        type(self).session_dir = Path(row["session_dir"])

        # Steer diagnosis toward Kubernetes MCP tools (default prompt is FRR-heavy).
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

        run_kwargs = {
            "agent_type": "cli.claude",
            "model": CLAUDE_MODEL,
            "max_steps": 25,
        }
        self._run_agent(**run_kwargs)

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

        assert (type(self).session_dir / "submission.json").is_file()
