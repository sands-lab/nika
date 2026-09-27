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

- run diagnosis using the diagnosis MCP tools, within `max_steps` LLM turns
- hand the report to the shared freeze step, then call `submit` with `resource_id` and `fault_type` pairs from the frozen submission context
- write useful trace events to `results/{session_id}/messages.jsonl`
- leave `submission.json` in the session directory through the task MCP `submit` tool

Subclass `agent.utils.two_phase.TwoPhaseAgent` to get this pipeline. You implement `diagnose()` and `submit()`. The base class writes the phase bookends (`agent_start`, then `agent_done` or `agent_error`), fails the run on an `ERROR:` diagnosis report, freezes the report, and advances the MCP gateway before it calls `submit()`. See [Step limit](agent-implementations.md#step-limit) for the `max_steps` contract.

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

from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.two_phase import TwoPhaseAgent
from nika.utils.session import Session


class MyAgent(TwoPhaseAgent):
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
        self.trace_dir = self.session.session_dir

    async def _tools(self, phase: str) -> dict[str, Any]:
        config = load_session_mcp_config(
            self.session_id, self.session.scenario_name, phase=phase
        )
        client = MultiServerMCPClient(connections=config)
        return {tool.name: tool for tool in await client.get_tools()}

    async def diagnose(self, task_description: str) -> str:
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        logger.log("llm_start", {"messages": {"role": "user", "content": task_description}})
        tools = await self._tools(DIAGNOSIS)

        # Replace this block with your framework or model loop.
        result = await tools["exec_shell"].ainvoke(
            {"host_name": "pc1", "command": "ping -c 2 195.11.14.1"}
        )
        diagnosis = f"Observed ping output: {result}"

        logger.log("llm_end", {"text": diagnosis, "model": self.model})
        return diagnosis

    async def submit(self, diagnosis_report: str, context: dict[str, Any]) -> str:
        logger = MessageLogger(phase=SUBMISSION, session_dir=self.trace_dir)
        tools = await self._tools(SUBMISSION)
        # Select resource_id and fault_type from context["resources"] and
        # context["fault_ontology"] (entries are {id, description, owner_kind}),
        # usually with your model or framework.
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
        return str(output)
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

NIKA exposes tools through the session MCP gateway (HTTP). Prefer the shared helper, which returns only the servers of one phase:

```python
from agent.utils.mcp_client import load_session_mcp_config

diagnosis_config = load_session_mcp_config(session_id, scenario_name, phase="diagnosis")
submission_config = load_session_mcp_config(session_id, scenario_name, phase="submission")
```

Common submission flow:

1. Return the report from `diagnose()`. `TwoPhaseAgent` calls `begin_submission_mcp_phase(session_id, diagnosis_report)` on the host, which freezes the report and advances the gateway.
2. Read the resource inventory and fault ontology from the `context` argument of `submit()`.
3. Call `submit` with `is_anomaly` and `root_causes: [{resource_id, fault_type}, ...]`.

The gateway's phase-advance endpoint needs a host-only secret. An agent that runs in a sandbox cannot advance the phase itself, so it cannot read the submission context before diagnosis ends.

The task server rejects IDs outside those catalogs. See [MCP servers](mcp-servers.md) for the server catalog and packet capture workflow, and [root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md) for the submit contract.

## Write trace logs

Use `MessageLogger` for JSONL traces:

```python
from agent.utils.loggers import MessageLogger
from agent.protocols import DIAGNOSIS

logger = MessageLogger(phase=DIAGNOSIS, session_dir=session.session_dir)
logger.log("tool_start", {"tool": {"name": "exec_shell"}, "input": {"host_name": "pc1", "command": "ip route"}})
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
uv run nika eval metrics --session_id <session_id>
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

- Agent class has `session_id` and `async run(task_description)` (inherited from `TwoPhaseAgent`).
- Registry maps a stable CLI id to the class.
- Diagnosis uses MCP tools instead of direct Docker/Kathara duplication.
- Submission selects IDs from the frozen submission context, then uses the task MCP `submit` tool.
- `messages.jsonl` and `submission.json` appear in the session result directory.
- `uv run nika benchmark run ... -a community.my_agent` completes for a small case.

## Add agent skills

Claude Code and Codex agents can load reusable instructions during diagnosis. [Configure agent skills](agent-skills.md) covers the library layout, `SKILL.md` format, registration, and tests. SADE keeps its separate library under `src/agent/community/sade/.claude/`.
