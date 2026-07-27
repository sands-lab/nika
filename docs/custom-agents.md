# Custom Agents

This guide shows how to implement an agent that runs through `nika agent run` and participates in benchmark runs.

## Contract

Every agent must satisfy `agent.protocols.TroubleshootingAgent`:

```python
class TroubleshootingAgent(Protocol):
    session_id: str

    async def run(self, task_description: str) -> dict[str, Any]: ...
```

The CLI creates the agent in `agent.registry.create_agent()`, then calls:

```python
await agent.run(task_description=session.task_description)
```

Expected behavior:

- run diagnosis using the Kathara MCP tools
- run submission using the task MCP tools
- call `submit` before returning
- write useful trace events to `results/{session_id}/messages.jsonl`
- leave `submission.json` in the session directory through the task MCP `submit` tool

## Recommended Structure

Place new implementations under `src/agent/community/<name>/` unless they are project-maintained backends.

```text
src/agent/community/my_agent/
|-- __init__.py
|-- agent.py
|-- config.py
`-- prompts.py
```

Minimal implementation (same MCP helpers as `mock`; run via `nika agent run`):

```python
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.utils.session import Session


class MyAgent:
    def __init__(
        self,
        session_id: str,
        model: str,
        max_steps: int = 20,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.stream_output = stream_output
        self.session = Session()
        self.session.load_running_session(session_id=session_id)

    async def run(self, task_description: str) -> dict[str, Any]:
        diagnosis = await self._diagnose(task_description)
        await self._submit(diagnosis)
        return {"diagnosis_report": diagnosis}

    async def _diagnose(self, task_description: str) -> str:
        logger = MessageLogger(agent=DIAGNOSIS, session_dir=self.session.session_dir)
        logger.log("llm_start", {"messages": {"role": "user", "content": task_description}})

        config = load_session_mcp_config(self.session_id, self.session.scenario_name)
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        # Replace this block with your framework or model loop.
        result = await tools["get_reachability"].ainvoke({})
        diagnosis = f"Observed reachability state: {result}"

        logger.log("llm_end", {"text": diagnosis, "model": self.model})
        return diagnosis

    async def _submit(self, diagnosis: str) -> None:
        logger = MessageLogger(agent=SUBMISSION, session_dir=self.session.session_dir)
        begin_submission_mcp_phase(self.session_id)

        config = load_session_mcp_config(self.session_id, self.session.scenario_name)
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        await tools["list_avail_problems"].ainvoke({})

        submission = {
            "is_anomaly": True,
            "faulty_devices": ["pc1"],
            "root_cause_name": ["link_down"],
        }
        logger.log("tool_start", {"tool": {"name": "submit"}, "input": submission})
        output = await tools["submit"].ainvoke(submission)
        logger.log("tool_end", {"output": str(output)})
```

Use `src/agent/mock/mock_agent.py` as a deterministic reference and existing `src/agent/byo/`, `src/agent/local_cli/`, or `src/agent/sdk/` packages as framework-specific references.

## Register The Agent

Add the agent id to `src/agent/registry.py`:

```python
case "community.my_agent":
    from agent.community.my_agent.agent import MyAgent

    return MyAgent(
        session_id=session_id,
        model=model,
        max_steps=max_steps,
        stream_output=stream_output,
    )
```

If the agent needs custom environment variables, resolve them in `config.py` and keep registry construction small.

## MCP Access

NIKA exposes tools through the session MCP gateway (HTTP). Prefer the shared helpers:

```python
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config

# Diagnosis (and submission after phase advance): all session servers
config = load_session_mcp_config(session_id, scenario_name)

# Before submission tools: advance gateway phase
begin_submission_mcp_phase(session_id)
```

Common submission flow:

1. call `list_avail_problems`
2. choose one or more root-cause ids
3. call `submit` with `is_anomaly`, `faulty_devices`, and `root_cause_name`

## Logging

Use `MessageLogger` for JSONL traces:

```python
from agent.utils.loggers import MessageLogger
from agent.utils.phases import DIAGNOSIS

logger = MessageLogger(agent=DIAGNOSIS, session_dir=session.session_dir)
logger.log("tool_start", {"tool": {"name": "ping_pair"}, "input": {"host_a": "pc1", "host_b": "pc2"}})
logger.log("tool_end", {"output": "success"})
```

For LangChain-based agents, use `AgentCallbackLogger` instead of manual event logging.

## Run Locally

Use the mock agent first to validate the lab and task:

```shell
uv run nika env run simple_bgp
uv run nika failure inject link_down --set host_name=pc1 --set intf_name=eth0
uv run nika agent run -a mock -m mock-v1
uv run nika session close -y
uv run nika eval metrics
```

Then run your agent:

```shell
uv run nika env run simple_bgp
uv run nika failure inject link_down --set host_name=pc1 --set intf_name=eth0
uv run nika agent run -a community.my_agent -m <model> -n 20
```

For benchmark mode:

```shell
uv run nika benchmark run simple_bgp --problem link_down \
  --set host_name=pc1 --set intf_name=eth0 \
  -a community.my_agent -m <model> -n 20
```

## Checklist

- Agent class has `session_id` and `async run(task_description)`.
- Registry maps a stable CLI id to the class.
- Diagnosis uses MCP tools instead of direct Docker/Kathara duplication.
- Submission uses the task MCP `submit` tool.
- `messages.jsonl` and `submission.json` appear in the session result directory.
- `uv run nika benchmark run ... -a community.my_agent` completes for a small case.

## Skills

Claude Code and Codex agents can load reusable skill libraries during the **diagnosis** phase. See **[Agent Skills](agent-skills.md)** for:

- default library layout under `src/agent/skills/`
- `NIKA_ENABLE_SKILLS` / `NIKA_SKILLS_DIR`
- how to author `SKILL.md` files and register them in `CLAUDE.md`

SADE (`community.sade`) ships a separate 15-skill library; see [`src/agent/community/sade/README.md`](../src/agent/community/sade/README.md).
