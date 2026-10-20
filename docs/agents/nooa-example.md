# NOOA agent example

This is the complete agent from [Integrate a custom agent](custom-agents.md), built on [NVIDIA-labs Object Oriented Agents](https://github.com/NVIDIA-NeMo/labs-OO-Agents) (NOOA). Copy it as a starting point for an agent on any framework.

NOOA runs model-generated Python. Like every agent, it runs in its own sandbox, which reaches only the lab's MCP tools and the model endpoint.

## Project files

`pyproject.toml`:

```toml
[project]
name = "nooa-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["nooa[mcp]==0.0.10", "litellm<1.106"]
```

`nika-agent.yaml`:

```yaml
id: nooa
class: nooa_agent.agent:NooaAgent
providers: [openai, custom]
```

`nooa_agent/agent.py`:

```python
"""Diagnosis agent backed by NOOA.

``diagnose`` hands the incident to a NOOA CodeAct agent that calls the lab's
MCP tools from Python.
"""

from nooa import Agent, set_default_strategy
from nooa.config import CodeActConfig
from nooa.errors import GenerationError
from nooa.mcp import MCPManager, MCPTool
from nooa.strategies import CodeActStrategy
from nooa.unifiedllm.registry import get_llm_client

from agent.base import DiagnosisAgent, max_steps_report
from agent.protocols import DIAGNOSIS
from agent.utils.loggers import MessageLogger


class NetworkInvestigator(Agent):
    """You are a network operations engineer troubleshooting a live network lab."""

    def __init__(self, servers: dict[str, MCPTool], **kwargs):
        super().__init__(**kwargs)
        self.servers: dict[str, MCPTool] = servers

    async def investigate(self, task: str) -> str:
        """Find the root cause of the incident described in `task`.

        `self.servers` maps MCP server names to tool objects, and `tools` lists
        each server's tools. Call `doc(self.servers[name])` for signatures, then
        await them, e.g. `await self.servers[name].exec_shell(...)`. Start broad
        (reachability, telemetry), then confirm on the suspect devices. Return
        a report that names the faulty devices, interfaces or links, the root
        cause, and the evidence for it, as soon as the evidence supports it.
        """
        tools = {
            name: [t for t in dir(server) if not t.startswith("_")]
            for name, server in self.servers.items()
        }
        ...


class NooaAgent(DiagnosisAgent):
    async def diagnose(self, task_description: str) -> str:
        log = MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        servers = {}
        for name, cfg in self.diagnosis_mcp_config().items():
            tool = await MCPManager.create_url_server(
                name, cfg["url"], headers=cfg.get("headers")
            )
            _trace_tool_calls(tool, log)
            servers[name] = tool

        # LiteLLM reads the standard OPENAI_* endpoint and key variables.
        llm = get_llm_client(f"openai/{self.model}", max_tokens=self.max_tokens)
        investigator = NetworkInvestigator(servers, llm=llm)
        last_turn = _trace_llm_turns(investigator, log)
        # max_steps is the number of LLM turns, CodeAct's max_iterations.
        set_default_strategy(
            CodeActStrategy(config=CodeActConfig(max_iterations=self.max_steps))
        )
        try:
            return await investigator.investigate(task_description)
        except GenerationError:
            # Out of turns: report the latest model text.
            return max_steps_report(
                log, max_steps=self.max_steps, latest_text=last_turn["text"]
            )


def _trace_tool_calls(tool: MCPTool, log: MessageLogger) -> None:
    """Record MCP calls as trace events."""
    invoke = tool._invoke

    async def traced(name, arguments):
        log.log("tool_start", {"tool": {"name": name}, "input": arguments})
        try:
            result = await invoke(name, arguments)
        except Exception as exc:
            log.log("tool_error", {"tool": {"name": name}, "error": str(exc)})
            raise
        log.log("tool_end", {"tool": {"name": name}, "output": str(result)})
        return result

    tool._invoke = traced


def _trace_llm_turns(investigator: Agent, log: MessageLogger) -> dict[str, str]:
    """Record one ``llm_end`` per model response.

    Returns a dict whose ``text`` is the latest non-empty model text.
    """
    usage = {}
    last_turn = {"text": ""}

    def on_complete(event) -> None:  # usage arrives before the output text
        usage.update(
            model=event.model_name,
            input_tokens=event.prompt_tokens,
            output_tokens=event.completion_tokens,
        )

    def on_output(event) -> None:
        model = usage.pop("model", None)
        log.log(
            "llm_end",
            {"text": event.content, "model": model, "usage_metadata": dict(usage)},
        )
        usage.clear()
        if event.content:
            last_turn["text"] = event.content

    investigator.event_manager.on("LLMComplete", on_complete)
    investigator.event_manager.on("LLMOutput", on_output)
    return last_turn
```

## What the extra code does

Compared with the [short version](custom-agents.md#1-write-your-agent), the agent adds:

| Addition | Why |
| --- | --- |
| `tools = {...}` before `...` | NOOA runs this code before the first model turn, so the model starts with the tool inventory instead of spending turns discovering it. |
| `_trace_tool_calls`, `_trace_llm_turns` | Write [trace events](agent-reference.md#trace-event-contract) to `messages.jsonl`, so the session trace shows each model turn and tool call. NIKA counts steps and tool calls from its own records either way. |
| `max_iterations=self.max_steps` and `GenerationError` | Apply NIKA's [step limit](agent-reference.md#step-limit) and, when it is reached, report the latest model text like NIKA's other agents. |

The event names `LLMComplete` and `LLMOutput` match NOOA 0.0.10; NOOA's development branch merges them into `LLMResponse`.

## Install and run

NIKA installs the project's dependencies in the agent's sandbox on every run. The `pyproject.toml` above pins LiteLLM because `1.106.0rc1` fails against NOOA's `openai` client.

```shell
uv run nika agent add ../nooa-agent
```

Point it at any OpenAI-compatible endpoint, for example a local vLLM server:

```yaml
# config/nooa.yaml
agent:
  type: nooa
  provider: custom
  model: nvidia/Qwen3.6-35B-A3B-NVFP4
  base_url: http://localhost:8000/v1
```

```shell
uv run nika agent run --run-config config/nooa.yaml --problem dc_clos_s_link_down
```
