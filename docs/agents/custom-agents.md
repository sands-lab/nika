# Integrate a custom agent

To test your own agent on NIKA's network incidents, write one Python file that diagnoses the incident. NIKA provides the environment, the MCP tools your agent investigates with, the submission step, and the scoring.

This guide builds an agent with [NOOA](https://github.com/NVIDIA-NeMo/labs-OO-Agents), NVIDIA's object-oriented agent framework. Other frameworks follow the same steps.

## How a run works

Every `nika agent run` goes through the same four steps:

1. **Start the environment and inject the failure.** NIKA deploys the network and breaks it, for example by taking a link down.
2. **Diagnose.** Your agent receives a description of the symptoms and investigates the live network through NIKA's [MCP tools](mcp-servers.md). It returns a written report.
3. **Submit.** NIKA freezes the report and converts it into a structured answer: which component failed and how. By default, your agent's model writes the answer. It uses only the report, and the network is no longer reachable.
4. **Score.** NIKA compares the answer with the failure it injected, then closes the environment.

Your agent implements step 2. NIKA handles the others.

## 1. Write your agent

A custom agent is one Python file in `src/agent/custom/`. The file name is the agent's name: `src/agent/custom/nooa.py` runs as agent type `custom.nooa`. You do not register the agent or edit NIKA.

The file defines a class that inherits `DiagnosisAgent` and implements `diagnose`. `diagnose` receives the incident description and returns the diagnosis report. `DiagnosisAgent` provides the run settings, such as `self.model` and `self.max_steps`. Its `diagnosis_mcp_config()` method returns the MCP servers your agent may use.

Create model clients from the standard `OPENAI_BASE_URL` and `OPENAI_API_KEY` variables (or `ANTHROPIC_*`). NIKA runs your agent in an isolated sandbox and points these variables at your provider.

The NOOA example has two classes. `NetworkInvestigator` is the NOOA agent that reasons about the network. `NooaAgent` is the class NIKA runs. It declares what its sandbox needs, connects the investigator to the MCP tools, and returns the report.

```python
from nooa import Agent
from nooa.mcp import MCPManager, MCPTool
from nooa.unifiedllm.registry import get_llm_client

from agent.base import DiagnosisAgent


class NetworkInvestigator(Agent):
    """You are a network operations engineer troubleshooting a live network lab."""

    def __init__(self, servers: dict[str, MCPTool], **kwargs):
        super().__init__(**kwargs)
        self.servers: dict[str, MCPTool] = servers

    async def investigate(self, task: str) -> str:
        """Find the root cause of the incident, using the MCP tools in `self.servers`.

        Return a report that names the faulty devices, interfaces or links,
        the root cause, and the evidence for it.
        """
        ...  # NOOA's model writes and runs Python here


class NooaAgent(DiagnosisAgent):
    dependencies = ["nooa[mcp]==0.0.10", "litellm<1.106"]
    requires_python = ">=3.12,<3.14"
    providers = ["openai", "custom"]

    async def diagnose(self, task_description: str) -> str:
        servers = {
            name: await MCPManager.create_url_server(
                name, cfg["url"], headers=cfg["headers"]
            )
            for name, cfg in self.diagnosis_mcp_config().items()
        }
        llm = get_llm_client(f"openai/{self.model}")
        investigator = NetworkInvestigator(servers, llm=llm)
        return await investigator.investigate(task_description)
```

Your framework is not installed in NIKA's environment. These class attributes are optional. They tell NIKA what to set up in the agent's sandbox:

| Attribute | Default | Meaning |
| --- | --- | --- |
| `dependencies` | none | pip requirements that NIKA installs from PyPI on every run. Pin the versions so every run installs the same packages. |
| `requires_python` | the sandbox's Python | Version specifier, such as `">=3.12,<3.14"`. When the sandbox's Python does not match, NIKA sets up a matching interpreter and installs the agent's packages in it. |
| `providers` | all | Providers your model client supports: `openai`, `anthropic`, `deepseek`, or `custom`. NIKA rejects any other value before the run starts. |

NIKA reads these attributes from the source without importing your file, so write them as literal values on a class that inherits `DiagnosisAgent` directly.

NIKA copies everything under `src/agent/` into the sandbox, and the agent must not learn which benchmark runs it. Do not name NIKA anywhere in the file, including comments.

The repository includes the complete NOOA agent as [`src/agent/custom/nooa.py`](../../src/agent/custom/nooa.py). It adds trace events and the step limit; see the [NOOA agent example](nooa-example.md).

## 2. Run it

For the NOOA example, configure an [OpenAI API key](../operations/agent-sandbox.md#api-keys), then select the agent and model in your configuration:

```shell
uv run nika config set agent.type=custom.nooa \
  agent.provider=openai agent.model=gpt-5-mini
uv run nika agent run --problem dc_clos_s_link_down
```

NIKA runs the agent in its sandbox, carries out every step above, and prints the score at the end. `uv run nika agent list` shows the agent and its providers.

For an OpenAI-compatible endpoint, use the [NOOA profile setup](nooa-example.md#run-it) instead. Custom agents use API credentials. Subscription logins apply only to the built-in Codex and Claude agents.

<details>
<summary>Example investigation from a real run</summary>

*The NOOA agent on the earlier DC Clos topology, running `dc_clos_s_link_down` with `nvidia/Qwen3.6-35B-A3B-NVFP4`; shortened from the recorded trace and report.*

```text
Tool: run_pingmesh_snapshot(count=3)
Result: client_0 → dns_pod0, webserver0_pod0: 100% loss; server-to-server paths healthy

Tool: exec_shell(host_name="client_0", command="ip addr show; ip route show …")
Tool: exec_shell(host_name="super_spine_router_0", command="ip addr show …")
Tool: exec_shell(host_name=…, command='vtysh -c "show bgp summary"') on each router

Report: The physical link between client_0:eth0 and super_spine_router_0:eth2 is
        DOWN, isolating client_0 from the fabric. BGP sessions are up, but
        192.168.0.0/24 is not advertised because eth2 is down.

Score: detection, root cause, and localization 1.0 (17 steps, 20 tool calls)
```

</details>

## Agents in their own project

An agent in `src/agent/custom/` lives in your NIKA checkout. If the agent has its own repository, or you want to version it separately, run it from that project instead. Register the project with `nika agent add`, which reads a `nika-agent.yaml` manifest, or pass its class as `-a module.path:ClassName`. The [registration reference](agent-reference.md#custom-agent-registration) describes both.
