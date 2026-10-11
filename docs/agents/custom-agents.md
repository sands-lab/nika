# Integrate a custom agent

Bring your own **investigation loop** or agent framework. NIKA provides the network lab, MCP tools, and evaluation.

## 1. Implement the investigation

Give your agent the task description and access to NIKA's [MCP tools](mcp-servers.md). Let it investigate, then return a diagnosis with supporting evidence.

Today, inherit the LangGraph harness to reuse its setup and submission adapter:

```python
from agent.byo.langgraph.react_agent import BasicReActAgent


class MyAgent(BasicReActAgent):
    async def diagnose(self, task_description: str) -> str:
        # Run your investigation loop and return its diagnosis report.
        raise NotImplementedError
```

The [diagnosis worker](../../src/agent/byo/langgraph/phases/diagnosis.py) shows how to connect tools.

> **Current contract:** setup and submission are inherited from LangGraph. A framework-independent base is tracked in [#172](https://github.com/sands-lab/nika/issues/172); see the [integration reference](agent-reference.md#custom-agent-integration).

## 2. Register your harness

Place it under `src/agent/community/my_agent/`. Add it to the [agent factory](../../src/agent/registry.py) and declare its supported providers in the [provider registry](../../src/agent/utils/provider_env.py). See [registration details](agent-reference.md#custom-agent-registration).

Set the provider, model, and limits in `config/nika.yaml`, following an [example configuration](../../config/byo.langgraph.example.yaml). Keep credentials in `.env`.

## 3. Run and check the result

```shell
uv run nika agent run -a community.my_agent --problem dc_clos_s_link_down
```

NIKA runs the incident, evaluates the answer, and closes the lab. The LangGraph harness prints progress in the terminal and records model output and tool calls in `messages.jsonl`.

<details>
<summary>Example investigation from a real run</summary>

*LangGraph with `nvidia/Qwen3.6-35B-A3B-NVFP4`; shortened from the recorded trace.*

```text
Model: Let me start by gathering comprehensive information about the network state.

Tool: run_pingmesh_snapshot(count=8, max_pairs=64)

Model: The ping mesh reveals a critical issue: client_0 is completely isolated
       from both dns_pod0 and webserver0_pod0 (100% packet loss in both directions).

Tool: exec_shell(host_name="client_0", command="ip addr show …")
Result: eth0 … state DOWN

Diagnosis: The root cause is a physical link failure between client_0
           and super_spine_router_0.
```

</details>
