# Agent reference

This reference lists the agents NIKA ships, the settings they share, and the limits every agent runs under. To set up an agent, see [Choose and run an agent](agent-implementations.md). To build one, see [Integrate a custom agent](custom-agents.md).

## Agent catalog

Every agent is listed by the name you pass to `-a`. Run `uv run nika agent list` to see the agents in your checkout.

| Agent | Built on | Sandbox base | Skills |
| --- | --- | --- | --- |
| `byo.langgraph` | LangGraph ReAct workers | `shell` | No |
| `byo.mcp_agent` | mcp-agent `Agent` workers | `shell` | No |
| `byo.autogen` | AutoGen `AssistantAgent` workers | `shell` | No |
| `cli.codex` | Codex CLI, two phases | `codex` | Yes |
| `cli.claude` | Claude Code CLI, two phases | `claude` | Yes |
| `cli.opencode` | OpenCode CLI, two phases | `shell` | Yes |
| `cli.dsh` | DeepSeek Harness `dsh --profile headless`, two phases | `shell` | Yes |
| `sdk.codex_sdk` | `openai-codex`, two threads | `shell` | Yes |
| `sdk.claude_sdk` | `claude-agent-sdk`, two sessions | `shell` | Yes |
| `community.sade` | Claude Agent SDK with a 15-skill library | `shell` | Own SADE library |

`oracle` and `mock` run inside the NIKA process without a model, so they are not for benchmark comparisons. `oracle` runs each task's reference diagnosis; see [Oracle solver](../benchmarks/oracle-solver.md). `mock` supports tests and pipeline checks, and `nika agent list` does not show it.

## Shared run contract

Every agent runs the same two phases:

1. **Diagnosis.** The agent investigates the live environment through the diagnosis MCP tools and writes a free-text report.
2. **Submission.** NIKA freezes the report and turns it into a structured answer. The answer has `is_anomaly` and `root_causes`, a list of `{resource_id, fault_type}` pairs.

During submission, the agent has no diagnosis tools, so it cannot investigate again. `agent.submission.mode` chooses who writes the answer:

| Mode | Who answers | Use it for |
| --- | --- | --- |
| `structured` (default) | The agent's own model calls the `submit` tool. | Leaderboard and comparable runs, where the score includes how well the agent's model submits its answer. |
| `unstructured` | One or more models set in `agent.submission` map the frozen report to an answer. | Comparing reports from agents that use different models, or none. |

For how the modes are scored, see [How NIKA evaluates an agent](../benchmarks/evaluation-pipeline.md). Unstructured runs record the mapping models in their run identity and cannot be packed for the leaderboard.

A completed run writes `messages.jsonl` and `submission.json` to its session result directory. The [root-cause scoring reference](../benchmarks/root-cause-evaluation.md) defines the submission schema.

> [!NOTE]
> An empty report, or one that starts with `ERROR:`, fails the run before submission.

## Run settings

Set shared values in `config/nika.yaml`, or override them on `nika agent run`:

| CLI flag | Configuration key | Default | Meaning |
| --- | --- | --- | --- |
| `-a`, `--agent` | `agent.type` | `byo.langgraph` | Agent to run. |
| `-p`, `--provider` | `agent.provider` | `openai` | Provider for model requests. |
| `-m`, `--model` | `agent.model` | None | Model ID. |
| `-n`, `--max-steps` | `agent.max_steps` | `20` | Maximum LLM requests per phase. See [Step limit](#step-limit). |
| (run config only) | `agent.timeout_sec` | `1800` | Wall-clock limit for the whole agent run, in seconds. |
| (run config only) | `agent.max_tokens` | `8192` | Output-token cap per model response. Codex agents and SADE do not apply it. |
| `-e`, `--reasoning-effort` | `agent.reasoning_effort` | `medium` for built-in harnesses | Reasoning effort. See [Reasoning effort](#reasoning-effort). |
| `--base-url` | `agent.base_url` | None | Endpoint URL for `custom` providers. |

See [Run configuration](../operations/configuration.md) for precedence and validation rules.

## Providers

Store provider keys in `.env`. Set the endpoint with `--base-url`, `nika config set agent.base_url=...`, or YAML.

| Agent family | Providers |
| --- | --- |
| `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `cli.opencode` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.codex`, `sdk.codex_sdk`, `cli.dsh` | `openai`, `deepseek`, `custom` |
| `cli.claude`, `sdk.claude_sdk`, `community.sade` | `anthropic`, `deepseek`, `custom` |

### Custom endpoint compatibility

Only `provider: custom` reads `agent.base_url`. Set it to the OpenAI-style API root, usually ending in `/v1`.

- OpenAI-family agents call the chat completions API.
- Claude-family agents call the Anthropic `/v1/messages` route. NIKA probes that route before it creates the sandbox. An HTTP 404 response fails the run with an endpoint error.

NIKA removes the trailing `/v1` when it configures an Anthropic client, because that client adds `/v1/messages` itself.

Every agent runs in a sandbox on Docker Sandboxes or E2B. The environment and MCP gateway stay on the host. See [Run agents in sandboxes](../operations/agent-sandbox.md).

## Reasoning effort

Built-in harnesses use `medium` when neither `-e` nor `agent.reasoning_effort` sets a value. `community.sade` and added agents get no default, and receive the configured value as is.

| Agent family | Accepted levels | How NIKA passes the level |
| --- | --- | --- |
| `byo.langgraph`, `byo.autogen`, `cli.codex`, `sdk.codex_sdk` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` | Passed to the harness. |
| `byo.mcp_agent` | `none`, `low`, `medium`, `high` | Passed to the harness. |
| `cli.claude`, `sdk.claude_sdk` | Same as Codex | `--effort`. `minimal` becomes `low`; `none` omits the flag. |
| `cli.opencode` | Same as Codex | `--variant`. `xhigh` becomes `max`; `none` omits the flag. |
| `cli.dsh` | Same as Codex | DeepSeek Harness thinking. `minimal` and `low` become `low`; `medium` and `high` become `high`; `xhigh` becomes `max`; `none` omits it. |

On the `custom` provider, `byo.langgraph` sends `reasoning_effort` only for `low`, `medium`, and `high`, the levels that OpenAI-compatible servers such as vLLM accept.

The effective value is recorded in the session, the benchmark `run.json`, and the leaderboard package identity.

## Sandbox credentials and packages

Codex agents accept an OpenAI API key or a ChatGPT login. Claude agents accept an Anthropic API key or a Claude login. Complete the [subscription login](../operations/agent-sandbox.md#subscriptions) on the NIKA host before you evaluate with a subscription. All families also support DeepSeek and compatible custom endpoints.

Each agent's Python packages are pinned for that agent and installed in its sandbox, not in NIKA's environment. See [agent packages](../operations/agent-sandbox.md#agent-packages-optional-offline-wheels) and [E2B templates](../operations/agent-sandbox.md#e2b-templates).

## Community agents

Community agents live in `src/agent/community/<name>/` and follow the shared agent contract. NIKA registers `community.sade`. Its [reference](community/sade.md) covers its models, credentials, and skills. The [community index](community/README.md) lists all community agents.

## Inspect the active interface

```shell
uv run nika agent list
uv run nika agent run --help
```

For every setting, see [Run configuration](../operations/configuration.md). For command behavior, see the [agent CLI reference](../operations/cli-reference.md#nika-agent). For the agent test matrix, see [Testing](../development/testing.md#agent-tests-testsagent).

## Step limit

`agent.max_steps` caps the LLM requests that one phase can make. NIKA's model proxy counts every generation request from the sandbox, including requests from sub-agents, and the `steps` metric counts the same requests. See [Model proxy and run records](../operations/agent-sandbox.md#model-proxy-and-run-records).

When diagnosis reaches the limit, the proxy refuses the next request. NIKA stops the agent, logs `max_steps_reached`, and freezes the latest assistant text as the report. Submission then runs as usual. If the model wrote no text before the limit, the report is `ERROR: ...` and the run fails.

Most harnesses also pass `max_steps` to their own turn limit, so they usually stop on their own and write their report:

| Agent | Harness limit |
| --- | --- |
| `byo.langgraph` | `ModelCallLimitMiddleware(run_limit=max_steps)` |
| `byo.mcp_agent` | mcp-agent `max_iterations` |
| `byo.autogen` | `max_tool_iterations = max_steps - 1`, plus one reflection turn that writes the report |
| `cli.claude` | `claude --max-turns` |
| `cli.opencode` | NIKA counts model responses in the `opencode run --format json` stream and stops OpenCode when a response past the limit starts |
| `cli.dsh` | NIKA counts model responses in the `dsh --profile headless --json` stream and stops the harness when a response past the limit starts |
| `cli.codex` | NIKA counts model responses in the `codex exec --json` stream and stops Codex when a response past the limit starts |
| `sdk.claude_sdk`, `community.sade` | Claude Agent SDK `max_turns` |
| `sdk.codex_sdk` | NIKA counts `thread/tokenUsage/updated` notifications, one per model response, and interrupts the turn past the limit |

## Trace event contract

Workers can show their investigation in the session trace by writing these events with `MessageLogger` (or `AgentCallbackLogger` for LangChain). The logger adds the timestamp and phase.

| Event | Worker payload | Meaning |
| --- | --- | --- |
| `llm_start` | `messages` | A model request begins. |
| `llm_end` | `text`, `model`, `usage_metadata` when available | A model response. Usage supports `input_tokens`, `output_tokens`, and `reasoning_tokens`. |
| `tool_start` | `tool: {name}`, `input`, `tool_call_id` when available | A tool invocation. |
| `tool_end` | Matching tool name or call ID, `output` | A tool result. |
| `tool_error` | Matching tool name or call ID, error details | A failed tool invocation. |

Record real usage and errors. Do not emit model events for deterministic code. NIKA counts steps and tool calls from its own records of model and MCP calls, and uses these events only when those records are missing. NIKA writes the submission phase's events itself, including each mapping attempt in unstructured mode. See [`loggers.py`](../../src/agent/utils/loggers.py) and [`usage.py`](../../src/agent/utils/usage.py) for payload normalization.

## Custom agent integration

Subclass [`DiagnosisAgent`](../../src/agent/base.py) and implement `diagnose(task_description) -> str`. The shortest path is one file, `src/agent/custom/<name>.py`, which runs as agent type `custom.<name>`. [Integrate a custom agent](custom-agents.md) walks through it.

The agent runs in its sandbox, one phase per process. The constructor reads the task from the sandbox workspace and sets these run settings:

- `self.model`, `self.llm_provider`, `self.max_steps`, `self.max_tokens`, `self.reasoning_effort`
- `self.session_id`, `self.scenario_name`
- `self.trace_dir`, the directory that holds `messages.jsonl`

`self.diagnosis_mcp_config()` returns the diagnosis MCP servers. Model clients read the standard `OPENAI_BASE_URL` and `OPENAI_API_KEY` (or `ANTHROPIC_*`) variables, which point at NIKA's model proxy. A subclass with its own constructor calls `super().__init__(session_id, ...)` first.

Submission needs no code. The default `submit(diagnosis_report, context)` uses the agent's `model` and `llm_provider`, so an agent without a provider must override `submit` or run in `unstructured` mode. Override `submit` only to keep a framework's own submission worker. It must answer from the report and context alone, through the task MCP `submit` tool; see the [Python MCP client](#python-mcp-client).

NIKA enforces the [step limit](#step-limit) and records model and MCP calls. Lifecycle events, report freezing, and submission mode belong to the base class and the host.

## Custom agent registration

NIKA describes each agent with one [`AgentSpec`](../../src/nika/agents/registry.py): the class to import, the accepted providers, credential routing, and sandbox setup. Provider checks, credential mapping, model defaults, and `nika agent list` all read that spec, so you do not edit NIKA to add an agent.

NIKA resolves `-a <name>` in this order:

1. Built-in harnesses.
2. `custom.<name>`: the file `src/agent/custom/<name>.py`. NIKA reads its class attributes `dependencies`, `requires_python`, and `providers` from the source, without importing it.
3. Agents registered with `nika agent add <project>` and recorded in `config/agents.yaml`.
4. A `module.path:ClassName` import path, with default settings. NIKA finds the module on its `sys.path`, for example through `PYTHONPATH`.

For an agent registered with `nika agent add`, NIKA copies the project directory, or its `src/` directory, into the sandbox and puts it on `PYTHONPATH`. The project is never imported or installed in NIKA's environment.

The class receives only the keyword arguments it declares among `session_id`, `model`, `llm_provider`, `max_steps`, `reasoning_effort`, `max_tokens`, and `stream_output`.

### Manifest fields

`nika agent add` reads `nika-agent.yaml` from the project directory:

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | required | Name for `-a`. Lowercase letters, digits, `.`, `_`, `-`. It must not match a built-in harness. |
| `class` | required | `module.path:ClassName` of the harness. `nika agent add` fails if the module does not define it. |
| `providers` | all | Providers the agent accepts. |
| `credential_family` | `openai` | Client family that receives `deepseek` and `custom` credentials: `OPENAI_*` or `ANTHROPIC_*`. |
| `supports_max_tokens` | `true` | Whether `agent.max_tokens` applies to the agent and is recorded for the run. |
| `default_model` | none | Model used when neither `-m` nor `agent.model` sets one. |
| `sandbox` | none | Sandbox definition, described below. |

### Sandbox definition

An added agent runs in a sandbox like the built-in ones. You set the base template, the packages to install, and the extra hosts that must be reachable during setup:

```yaml
sandbox:
  base: shell                      # shell (default), claude, or codex
  install: ["nooa[mcp]==0.0.10"]   # default: [project].dependencies
  egress: [download.pytorch.org]   # reachable during setup only
```

Pin versions in `install` or in `pyproject.toml` so runs stay reproducible. On Docker Sandboxes, `egress` hosts are reachable only while the packages install. After setup, the sandbox reaches the session MCP gateway and the host model proxy. E2B installs packages into its template, so `egress` does not apply. See [network egress](../operations/agent-sandbox.md#how-sandboxes-stay-isolated). The `nika-agent.yaml` file stays on the host.

## Python MCP client

If your framework has no MCP adapter, use the standard Python client with the connections from `self.diagnosis_mcp_config()`. This example invokes one tool that your model chose:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def invoke_connection(connection: dict, name: str, arguments: dict):
    async with streamablehttp_client(
        connection["url"], headers=connection.get("headers")
    ) as (read, write, _):
        async with ClientSession(
            read, write, **connection.get("session_kwargs", {})
        ) as client:
            await client.initialize()
            tools = await client.list_tools()
            # In your loop, give tools.tools to the model before choosing a call.
            # Each tool supplies name, description, and inputSchema.
            if name not in {tool.name for tool in tools.tools}:
                raise ValueError(f"Unknown tool: {name}")
            result = await client.call_tool(name, arguments=arguments)
            return result
```

In a full loop, discover the tools across all connections, keep track of which server each tool belongs to, and keep the client sessions open. Feed `result.content` and `result.isError` back to the model before its next turn. Choose arguments from the discovered schemas rather than hardcoding a topology-specific host or address.

For submission, load the connections with `agent.utils.mcp_client.session_mcp_config("submission")` and call `submit` with the answer chosen from the frozen report and catalogs. Raise on `result.isError`, because a failed call does not record an answer.
