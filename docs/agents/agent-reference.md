# Agent configuration and telemetry reference

Use [Choose and run an agent](agent-implementations.md) for setup walkthroughs and [Custom agent integration](custom-agents.md) to implement an agent. This reference records provider support, limits, telemetry semantics, and implementation details.

[`protocols.py`](../../src/agent/protocols.py) defines the shared contract. [`registry.py`](../../src/agent/registry.py) maps the CLI names below to their implementations. Confirm the installed checkout with `uv run nika agent list`.

## Agent catalog

| CLI name | Orchestration | Execution | Skill support |
| --- | --- | --- | --- |
| `byo.langgraph` | LangGraph ReAct workers | Host | No |
| `byo.mcp_agent` | mcp-agent `Agent` workers | Host | No |
| `byo.autogen` | AutoGen `AssistantAgent` workers | Host | No |
| `cli.codex` | Codex CLI, two phases | Sandbox, `codex` template | Shared Codex skills |
| `cli.claude` | Claude Code CLI, two phases | Sandbox, `claude` template | Shared Claude skills |
| `sdk.codex_sdk` | `openai-codex`, two threads | Sandbox, `shell` template | Shared Codex skills |
| `sdk.claude_sdk` | `claude-agent-sdk`, two sessions | Sandbox, `shell` template | Shared Claude skills |
| `community.sade` | Claude Agent SDK with a 15-skill library | Sandbox, `shell` template | SADE library |

The deterministic `mock` agent supports tests and pipeline checks. Do not use it for benchmark comparisons.

## Shared run contract

Every agent runs the same pipeline:

1. **Diagnosis.** The agent inspects the live lab through the diagnosis MCP servers and writes a free-text report.
2. **Freeze and advance.** The host records the report as `diagnosis_frozen` in `messages.jsonl` and moves the MCP gateway to the submission phase. The gateway then returns the fault ontology and the resource catalog.
3. **Submission.** The frozen report and that context become the answer recorded by the task MCP `submit` tool. Submission has no diagnosis tools, so it cannot investigate again. `agent.submission.mode` selects who answers:

| Mode | Who answers | Use it for |
| --- | --- | --- |
| `structured` (default) | The agent's own model, with only the `submit` tool. Built-in harnesses use their own submission worker; other agents get NIKA's default, the `byo.langgraph` submission agent. | Leaderboard and comparable runs: the score includes how well the agent's model fills the answer. |
| `unstructured` | A separate model set in `agent.submission`, the same for every agent type. It returns a JSON answer for each of `samples` calls; NIKA submits the majority answer once. | Comparing diagnosis reports when agents use different models or none, independent of each model's tool calling. |

Unstructured runs record the submission model in the run identity and cannot be packed for the leaderboard.

A completed run writes `messages.jsonl` and `submission.json` under the session result directory. The [root-cause scoring reference](../benchmarks/root-cause-evaluation.md) defines the submission schema.

> **Failed diagnosis:** an empty report, or one starting with `ERROR:`, fails the run before submission.

## Run settings

Configure shared values in `config/nika.yaml` or override them on `nika agent run`:

| CLI flag | Configuration key | Schema default |
| --- | --- | --- |
| `-a`, `--agent` | `agent.type` | `byo.langgraph` |
| `-p`, `--provider` | `agent.provider` | `openai` |
| `-m`, `--model` | `agent.model` | None |
| `-n`, `--max-steps` | `agent.max_steps` | `20`; LLM turns per phase, for every agent (see [Step limit](#step-limit)) |
| (run config only) | `agent.timeout_sec` | `1800`; wall-clock budget for the whole agent run, for every agent |
| (run config only) | `agent.max_tokens` | `8192`; output-token cap per model response, for every LLM agent except `cli.codex`, `sdk.codex_sdk`, and `community.sade` |
| `-e`, `--reasoning-effort` | `agent.reasoning_effort` | None |
| `--base-url` | `agent.base_url` | None |

See [Run configuration](../operations/configuration.md) for precedence, defaults, and validation rules.

### Step limit

`max_steps` is the number of LLM turns (model responses) a phase may use. The evaluator's `steps` metric counts the same unit: one `llm_end` event per model response in `messages.jsonl`.

When diagnosis uses all `max_steps` turns, the agent stops, logs `max_steps_reached`, and freezes its latest assistant text as the report. The submission phase then runs as usual. If the agent wrote no text before the limit, the report is `ERROR: ...` and the run fails.

| Agent | How NIKA enforces `max_steps` |
| --- | --- |
| `byo.langgraph` | `ModelCallLimitMiddleware(run_limit=max_steps)` |
| `byo.mcp_agent` | mcp-agent `max_iterations` |
| `byo.autogen` | `max_tool_iterations = max_steps - 1`, plus one reflection turn that writes the report |
| `cli.claude` | `claude --max-turns` |
| `cli.codex` | NIKA counts model responses in the `codex exec --json` stream and stops Codex when a response past the limit starts |
| `sdk.claude_sdk`, `community.sade` | Claude Agent SDK `max_turns` |
| `sdk.codex_sdk` | NIKA counts `thread/tokenUsage/updated` notifications, one per model response, and interrupts the turn past the limit |
| `mock` | Not applicable (no LLM) |

<details>
<summary>Codex CLI step and token accounting</summary>

`codex exec` reports no event per model response. `cli.codex` starts a new step at the first item after all tool calls of the previous response finished. Tool calls that Codex runs one after another from a single response therefore count as separate steps, so `cli.codex` can report more steps than the model made. `codex exec` also reports token usage once per run, so `cli.codex` puts all tokens on the last `llm_end` of the phase.

</details>

## Providers

Store provider keys in `.env`. Set custom endpoint URLs with `--base-url`, `nika config set agent.base_url=...`, or YAML.

| Agent family | Providers |
| --- | --- |
| `byo.langgraph`, `byo.mcp_agent`, `byo.autogen` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.codex`, `sdk.codex_sdk` | `openai`, `deepseek`, `custom` |
| `cli.claude`, `sdk.claude_sdk`, `community.sade` | `anthropic`, `deepseek`, `custom` |

### Custom endpoint compatibility

Only `provider: custom` reads `agent.base_url`. Set it to the OpenAI-style API root, usually ending in `/v1`. OpenAI-family agents use chat completions; Claude-family agents require the Anthropic `/v1/messages` route. NIKA probes that route with an empty POST before creating a Claude-family sandbox; HTTP 404 fails the run with an endpoint error. The provider adapter strips the trailing `/v1` when configuring an Anthropic client, which appends `/v1/messages` itself.

CLI, SDK, and SADE agents run in sandboxes, on Docker Sandboxes or E2B. The lab and MCP gateway remain on the host. See [Run agents in sandboxes](../operations/agent-sandbox.md) for setup, credentials, isolation, proxy settings, and troubleshooting.

## Reasoning effort

`byo.langgraph`, `byo.autogen`, `cli.codex`, and `sdk.codex_sdk` accept `none`, `minimal`, `low`, `medium`, `high`, or `xhigh`. `byo.mcp_agent` accepts `none`, `low`, `medium`, or `high`. Claude-family agents ignore the shared `-e` option.

## Sandbox credentials and dependencies

Codex agents accept an OpenAI API key or a ChatGPT login; Claude agents accept an Anthropic API key or a Claude login. On sbx, the SDK and SADE agents need the API key. Complete the [subscription login](../operations/agent-sandbox.md#subscription--oauth-interactive-once) for your sandbox runtime before evaluating with a subscription. Both families also support DeepSeek and compatible custom endpoints. Claude uses `--bare` with environment credentials and its stored login in subscription mode.

Install SDK agents with `uv sync --extra sdk --prerelease=allow`. On sbx, their sandboxes install host-cached wheels by default; set `nika.sandbox.offline_sdk_wheels: false` to use PyPI. See [SDK offline wheels](../operations/agent-sandbox.md#sdk-agents-optional-offline-wheels). On E2B, the [template](../operations/agent-sandbox.md#e2b-templates) already contains them.

## Community agents

Community implementations live under `src/agent/community/<name>/` and implement the shared agent contract. NIKA registers `community.sade`; its [reference](community/sade.md) covers dependencies, models, credentials, and skills.

## LangGraph observability

`byo.langgraph` can send traces to Langfuse. Install `uv sync --extra observability`, set `nika.observability.langfuse_enabled: true`, and put `LANGFUSE_SECRET_KEY` and `LANGFUSE_PUBLIC_KEY` in `.env`. Set a non-default host with `nika.observability.langfuse_host`.

## Inspect the active interface

```shell
uv run nika agent list
uv run nika agent run --help
```

Use [Run configuration](../operations/configuration.md) for every setting, [CLI](../operations/cli-reference.md#nika-agent) for command behavior, and [Testing](../development/testing.md#agent-tests-testsagent) for the agent test matrix.

## Trace event contract

`NikaAgent` writes each phase's lifecycle events. The submission `agent_start` event records `submission_mode`, plus the mapping `provider`, `model`, `samples`, and `temperature` in `unstructured` mode. Unstructured runs also record each mapping attempt as `submission_sample` (`sample`, `attempt`, raw `text`, parsed `answer` or `error`) and the outcome as `submission_vote` (per-sample `answers`, `votes` with counts, and the `chosen` answer). Workers record real model and tool calls using `MessageLogger` (or `AgentCallbackLogger` for LangChain). The evaluator normally measures diagnosis-phase events only.

| Event | Worker payload | Meaning |
| --- | --- | --- |
| `llm_start` | `messages` | Model request begins. |
| `llm_end` | `text`, `model`, `usage_metadata` when available | One actual model response; increments the step metric. Usage supports `input_tokens`, `output_tokens`, and `reasoning_tokens` through the shared normalizer. |
| `tool_start` | `tool: {name}`, `input`, `tool_call_id` when available | Tool invocation; increments tool-call count. |
| `tool_end` | Matching tool name/call ID, `output` | Tool result. |
| `tool_error` | Matching tool name/call ID, error details | Failed tool invocation; increments tool-error count. |

The logger supplies timestamp and phase. Preserve real usage and errors; do not emit synthetic model events for deterministic code. See [`loggers.py`](../../src/agent/utils/loggers.py), [`usage.py`](../../src/agent/utils/usage.py), and [`trace_parser.py`](../../src/nika/evaluator/trace_parser.py) for supported payload normalization and native CLI/SDK transcript handling.

## Custom agent integration

Subclass [`NikaAgent`](../../src/agent/base.py) and implement `diagnose(task_description) -> str`. The constructor loads the running session and keeps the run settings: `self.model`, `self.llm_provider`, `self.max_steps`, `self.max_tokens`, `self.reasoning_effort`, `self.session_id`, `self.scenario_name`, and `self.trace_dir` (where `messages.jsonl` lives). `self.diagnosis_mcp_config()` returns the diagnosis MCP servers. A subclass that needs its own constructor calls `super().__init__(session_id, ...)` first.

Submission needs no code: NIKA answers from the frozen report as described in [the shared run contract](#shared-run-contract). The default `submit(diagnosis_report, context)` uses the agent's `model` and `llm_provider`, so an agent without a provider must override `submit` or run in `unstructured` mode. Override `submit` only to keep a framework's own submission worker; it must answer from the report and context alone, through the task MCP `submit` tool ([Python MCP client](#python-mcp-client)).

Workers enforce the [step limit](#step-limit) and record calls using the [trace event contract](#trace-event-contract). Lifecycle events, report freezing, and submission mode belong to the base class and host.

## Custom agent registration

NIKA describes every agent with one [`AgentSpec`](../../src/agent/registry.py): the class to import, accepted providers, credential routing, and sandbox wiring. Provider validation, credential mapping, model defaults, and `nika agent list` all read the spec, so an agent never needs edits elsewhere in NIKA.

NIKA resolves `-a <name>` in this order:

1. Built-in harnesses in `BUILTIN_AGENTS`.
2. Agents registered with `nika agent add <project>` and recorded in `config/agents.yaml`. NIKA puts the project directory, or its `src/` directory, on `sys.path` before importing the class, so the project is never installed into NIKA's environment.
3. A `module.path:ClassName` import path, with default settings. The module must already be importable, for example through `PYTHONPATH`.

The class receives the keyword arguments it declares among `session_id`, `model`, `llm_provider`, `max_steps`, `reasoning_effort`, `max_tokens`, and `stream_output`. Settings it does not declare are not passed.

### Manifest fields

`nika agent add` reads `nika-agent.yaml` from the project directory:

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | required | Name for `-a`. Lowercase letters, digits, `.`, `_`, `-`; must not match a built-in harness. |
| `class` | required | `module.path:ClassName` of the harness. |
| `providers` | all | Providers accepted by CLI and YAML validation. |
| `credential_family` | `openai` | Client family that receives `deepseek` and `custom` credentials (`OPENAI_*` or `ANTHROPIC_*`). |
| `supports_max_tokens` | `true` | Whether `agent.max_tokens` applies and is recorded for the run. |
| `default_model` | none | Model used when neither `-m` nor `agent.model` sets one. |
| `sandbox` | none | Sandbox definition, described below. |

`nika agent add` fails if NIKA cannot import `class` or if a dependency listed in the project's `pyproject.toml` is missing from NIKA's environment.

### Sandbox definition

An agent can declare how it would be installed in an isolated sandbox. NIKA keeps the network policy, credentials, and phase boundary; the agent only states what to install and which LLM hosts it must reach:

```yaml
sandbox:
  base: python:3.12-slim
  install:
    - pip install /nika/agent-src
  egress: [integrate.api.nvidia.com]
  # entrypoint: optional; defaults to NIKA's in-sandbox runner, which imports class
```

NIKA validates this definition but does not execute it yet: external agents run on the host, and `nika agent list` marks declared sandboxes. Built-in sandbox harnesses keep their [sandbox wiring](../operations/agent-sandbox.md#architecture) in their specs.

## Python MCP client

If your framework has no MCP adapter, use the standard Python client with connections from `load_session_mcp_config()`. This example invokes one tool chosen by your model:

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

The snippet shows one invocation with a tool name and arguments chosen by your model. In a full loop, discover tools across the returned connections, retain the mapping to each server, and keep client sessions open. Feed `result.content` and `result.isError` back to the model before its next turn. Choose arguments from the discovered schemas rather than hardcoding a topology-specific host or address.

For submission, load connections with `phase="submission"` and invoke `submit` with the answer selected from the frozen report and catalogs. Raise on `result.isError`; a failed tool call does not record a valid answer.
