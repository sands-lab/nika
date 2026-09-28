# Agent implementation reference

This reference helps operators choose a registered troubleshooting agent and configure its provider, model, and execution environment. Use [Custom agent integration](custom-agents.md) to implement a new agent.

[`protocols.py`](../../src/agent/protocols.py) defines the shared contract. [`registry.py`](../../src/agent/registry.py) maps the CLI names below to their implementations. Confirm the installed checkout with `uv run nika agent list`.

## Agent catalog

| CLI name | Orchestration | Execution | Skill support |
| --- | --- | --- | --- |
| `byo.langgraph` | LangGraph ReAct workers | Host | No |
| `byo.mcp_agent` | mcp-agent `Agent` workers | Host | No |
| `byo.autogen` | AutoGen `AssistantAgent` workers | Host | No |
| `cli.codex` | Codex CLI, two phases | Docker Sandbox `codex` template | Shared Codex skills |
| `cli.claude` | Claude Code CLI, two phases | Docker Sandbox `claude` template | Shared Claude skills |
| `sdk.codex_sdk` | `openai-codex`, two threads | Docker Sandbox `shell` template | Shared Codex skills |
| `sdk.claude_sdk` | `claude-agent-sdk`, two sessions | Docker Sandbox `shell` template | Shared Claude skills |
| `community.sade` | Claude Agent SDK with a 15-skill library | Docker Sandbox `shell` template | SADE library |

The deterministic `mock` agent supports tests and pipeline checks. Do not use it for benchmark comparisons.

## Shared run contract

Every agent runs the same pipeline:

1. **Diagnosis.** The agent inspects the live lab through the diagnosis MCP servers and writes a free-text report.
2. **Freeze and advance.** The host records the report as `diagnosis_frozen` in `messages.jsonl` and moves the MCP gateway to the submission phase. The gateway then returns the fault ontology and the resource catalog.
3. **Submission.** The agent gets the frozen report and that context, with only the task MCP server, and calls `submit`.

A completed run writes `messages.jsonl` and `submission.json` under the session result directory. The [root-cause scoring reference](../benchmarks/root-cause-evaluation.md) defines the submission schema.

A diagnosis report that starts with `ERROR:` fails the run, and the submission phase does not start.

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
| `--base-url` | `agent.custom.base_url` | None |

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

`codex exec` reports no event per model response. `cli.codex` starts a new step at the first item after all tool calls of the previous response finished. Tool calls that Codex runs one after another from a single response therefore count as separate steps, so `cli.codex` can report more steps than the model made. `codex exec` also reports token usage once per run, so `cli.codex` puts all tokens on the last `llm_end` of the phase. Store provider keys in `.env`. Set custom endpoint URLs with `--base-url`, `nika config set agent.custom.base_url=...`, or YAML.

| Agent family | Providers |
| --- | --- |
| `byo.langgraph`, `byo.mcp_agent`, `byo.autogen` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.codex`, `sdk.codex_sdk` | `openai`, `deepseek`, `custom` |
| `cli.claude`, `sdk.claude_sdk`, `community.sade` | `anthropic`, `deepseek`, `custom` |

CLI, SDK, and SADE agents run in Docker Sandboxes. The lab and MCP gateway remain on the host. See [Docker Sandbox execution](../operations/agent-sandbox.md) for installation, credentials, isolation, proxy settings, and troubleshooting.

## BYO framework agents

BYO agents run on the host and use one framework-specific worker per diagnosis or submission phase.

| Agent | Entry point | Reasoning effort |
| --- | --- | --- |
| `byo.langgraph` | `agent.byo.langgraph.react_agent.BasicReActAgent` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |
| `byo.mcp_agent` | `agent.byo.mcp_agent.agent.McpAgent` | `none`, `low`, `medium`, `high` |
| `byo.autogen` | `agent.byo.autogen.agent.AutogenAgent` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |

```yaml
agent:
  type: byo.langgraph
  provider: deepseek
  model: deepseek-v4-flash
  max_steps: 20
  reasoning_effort: medium
```

```shell
uv run nika agent run
uv run nika agent run -a byo.langgraph -p deepseek -m deepseek-v4-flash -n 20
uv run nika agent run -a byo.mcp_agent -p deepseek -m deepseek-v4-flash -e low
```

### OpenAI-compatible endpoints

Use the `custom` provider for Ollama, vLLM, OpenRouter, or another OpenAI-compatible server:

```yaml
agent:
  type: byo.langgraph
  provider: custom
  model: qwen2.5:7b
  custom:
    base_url: http://localhost:11434/v1
```

```shell
uv run nika agent run -a byo.langgraph -p custom -m qwen2.5:7b \
  --base-url http://localhost:11434/v1 --problem dc_clos_s_link_down
uv run nika config set agent.provider=custom agent.model=qwen2.5:7b \
  agent.custom.base_url=http://localhost:11434/v1
```

Set `NIKA_CUSTOM_API_KEY` in `.env` only when the endpoint requires authentication.

## CLI agents

| Agent | Sandbox command | Model | Authentication |
| --- | --- | --- | --- |
| `cli.codex` | `codex exec` | `agent.model` | OpenAI API key, Codex OAuth, DeepSeek key, or custom endpoint |
| `cli.claude` | `claude -p` | `agent.model` | Anthropic API key, Claude login, DeepSeek key, or custom endpoint |

```shell
uv run nika agent run -a cli.codex -m gpt-5-mini -e medium
uv run nika agent run -a cli.claude -p deepseek -m deepseek-v4-flash
```

Codex accepts `none`, `minimal`, `low`, `medium`, `high`, or `xhigh` reasoning effort. Claude agents ignore the shared `-e` option. Claude runs with `--bare` when it uses environment credentials and uses its stored login in subscription mode.

## SDK agents

Install SDK agents with:

```shell
uv sync --extra sdk --prerelease=allow
```

| Agent | SDK | Model |
| --- | --- | --- |
| `sdk.codex_sdk` | `openai-codex` | `agent.model` |
| `sdk.claude_sdk` | `claude-agent-sdk` | `agent.model` |

Both SDK agents follow the shared [step limit](#step-limit).

```shell
uv run nika agent run -a sdk.codex_sdk -m gpt-5-mini -e medium
uv run nika agent run -a sdk.claude_sdk -p deepseek -m deepseek-v4-flash -n 20
```

SDK sandboxes install dependencies from host-cached wheels by default. Set `nika.sandbox.offline_sdk_wheels: false` to install them from PyPI instead. See [SDK offline wheels](../operations/agent-sandbox.md#sdk-agents-optional-offline-wheels).

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
