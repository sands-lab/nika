# Integrate a custom agent

This guide is for agent developers who want an implementation to run through `nika agent run` and participate in benchmark runs.

Core contracts: [`protocols.py`](../../src/agent/protocols.py) defines the interface, and [`registry.py`](../../src/agent/registry.py) registers CLI names.

## Implement the agent contract

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

- run diagnosis using the session MCP tools
- advance to the submission phase, then call `submit` with `resource_id` and `fault_type` pairs from the frozen submission context
- write useful trace events to `results/{session_id}/messages.jsonl`
- leave `submission.json` in the session directory through the task MCP `submit` tool

## Use the recommended structure

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
from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.utils.session import Session
from nika.workflows.agent.submission import load_submission_context


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
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.session.session_dir)
        logger.log("llm_start", {"messages": {"role": "user", "content": task_description}})

        config = load_session_mcp_config(self.session_id, self.session.scenario_name)
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        # Replace this block with your framework or model loop.
        result = await tools["ping_pair"].ainvoke(
            {"host_a": "pc1", "host_b": "pc2", "count": 2}
        )
        diagnosis = f"Observed ping_pair output: {result}"

        logger.log("llm_end", {"text": diagnosis, "model": self.model})
        return diagnosis

    async def _submit(self, diagnosis: str) -> None:
        logger = MessageLogger(phase=SUBMISSION, session_dir=self.session.session_dir)
        begin_submission_mcp_phase(self.session_id, diagnosis)

        config = load_session_mcp_config(self.session_id, self.session.scenario_name)
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        context = load_submission_context(self.session_id)
        # Select resource_id and fault_type from context["resources"] and
        # context["fault_ontology"] entries are {id, description, owner_kind}
        # from ownership_entries; usually via your model or framework.
        submission = {
            "is_anomaly": True,
            "root_causes": [
                {
                    "resource_id": context["resources"][0]["id"],
                    "fault_type": context["fault_ontology"][0]["id"],
                }
            ],
        }
        logger.log("tool_start", {"tool": {"name": "submit"}, "input": submission})
        output = await tools["submit"].ainvoke(submission)
        logger.log("tool_end", {"output": str(output)})
```

Use `src/agent/mock/mock_agent.py` as a deterministic reference and existing `src/agent/byo/`, `src/agent/cli/`, or `src/agent/sdk/` packages as framework-specific references.

## Register the agent

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

## Configure MCP access

NIKA exposes tools through the session MCP gateway (HTTP). Prefer the shared helpers:

```python
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config

# Diagnosis (and submission after phase advance): all session servers
config = load_session_mcp_config(session_id, scenario_name)

# Before submission tools: freeze diagnosis and advance gateway phase
begin_submission_mcp_phase(session_id, diagnosis_report)
```

Common submission flow:

1. Call `begin_submission_mcp_phase(session_id, diagnosis_report)`.
2. Read the frozen resource inventory and fault ontology from the submission context in the prompt (or `load_submission_context`).
3. Call `submit` with `is_anomaly` and `root_causes: [{resource_id, fault_type}, ...]`.

The task server rejects IDs outside those catalogs. See [MCP servers](mcp-servers.md) for the server catalog and packet capture workflow, and [root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md) for the submit contract.

## Write trace logs

Use `MessageLogger` for JSONL traces:

```python
from agent.utils.loggers import MessageLogger
from agent.protocols import DIAGNOSIS

logger = MessageLogger(phase=DIAGNOSIS, session_dir=session.session_dir)
logger.log("tool_start", {"tool": {"name": "ping_pair"}, "input": {"host_a": "pc1", "host_b": "pc2"}})
logger.log("tool_end", {"output": "success"})
```

For LangChain-based agents, use `AgentCallbackLogger` instead of manual event logging.

## Run locally

Use the mock agent first to validate the lab and task:

```shell
uv run nika env run dc_clos -s s
uv run nika failure inject link_down --set host_name=pc_0_0 --set intf_name=eth0
uv run nika agent run -a mock -m mock-v1
uv run nika session close -y
uv run nika eval metrics
```

Then run your agent:

```shell
uv run nika env run dc_clos -s s
uv run nika failure inject link_down --set host_name=pc_0_0 --set intf_name=eth0
uv run nika agent run -a community.my_agent -m <model> -n 20
```

For benchmark mode:

```shell
uv run nika benchmark run dc_clos -s s --problem link_down \
  --set host_name=pc_0_0 --set intf_name=eth0 \
  -a community.my_agent -m <model> -n 20
```

## Validate the integration

- Agent class has `session_id` and `async run(task_description)`.
- Registry maps a stable CLI id to the class.
- Diagnosis uses MCP tools instead of direct Docker/Kathara duplication.
- Submission selects IDs from the frozen submission context, then uses the task MCP `submit` tool.
- `messages.jsonl` and `submission.json` appear in the session result directory.
- `uv run nika benchmark run ... -a community.my_agent` completes for a small case.

## Add agent skills

Claude Code and Codex agents can load reusable instructions during diagnosis. [Configure agent skills](agent-skills.md) covers the library layout, `SKILL.md` format, registration, and tests. SADE keeps its separate library under `src/agent/community/sade/.claude/`.
