# Agent implementation reference

This reference helps operators choose a registered troubleshooting agent and configure its provider, model, and execution environment. Use [Custom agent integration](custom-agents.md) to implement a new agent.

[`protocols.py`](../../src/agent/protocols.py) defines the shared contract. [`registry.py`](../../src/agent/registry.py) maps the CLI names below to their implementations. Confirm the installed checkout with `uv run nika agent list`.

## Agent catalog

| CLI name | Orchestration | Execution | Skill support |
| --- | --- | --- | --- |
| `byo.langgraph` | LangGraph ReAct workers | Host | No |
| `byo.mcp_agent` | mcp-agent `Workflow` | Host | No |
| `byo.autogen` | AutoGen `GraphFlow` | Host | No |
| `cli.codex` | Codex CLI, two phases | Docker Sandbox `codex` template | Shared Codex skills |
| `cli.claude` | Claude Code CLI, two phases | Docker Sandbox `claude` template | Shared Claude skills |
| `sdk.codex_sdk` | `openai-codex`, two threads | Docker Sandbox `shell` template | Shared Codex skills |
| `sdk.claude_sdk` | `claude-agent-sdk`, two sessions | Docker Sandbox `shell` template | Shared Claude skills |
| `community.sade` | Claude Agent SDK with a 15-skill library | Docker Sandbox `shell` template | SADE library |

The deterministic `mock` agent supports tests and pipeline checks. Do not use it for benchmark comparisons.

## Shared run contract

Each agent diagnoses the live lab through MCP tools, advances to the submission phase, and calls the task MCP `submit` tool. A completed run writes `messages.jsonl` and `submission.json` under the session result directory. The [root-cause scoring reference](../benchmarks/root-cause-evaluation.md) defines the submission schema.

Configure shared values in `config/nika.yaml` or override them on `nika agent run`:

| CLI flag | Configuration key | Schema default |
| --- | --- | --- |
| `-a`, `--agent` | `agent.type` | `byo.langgraph` |
| `-p`, `--provider` | `agent.provider` | `openai` |
| `-m`, `--model` | `agent.model` | None |
| `-n`, `--max-steps` | `agent.max_steps` | `20`; used by BYO agents, Claude SDK, SADE, and `mock` |
| `-e`, `--reasoning-effort` | `agent.reasoning_effort` | None |
| `--base-url` | `agent.custom.base_url` | None |

See [Run configuration](../operations/configuration.md) for precedence, defaults, and validation rules. Store provider keys in `.env`. Set custom endpoint URLs with `--base-url`, `nika config set agent.custom.base_url=...`, or YAML.

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

| Agent | SDK | Model | Step limit |
| --- | --- | --- | --- |
| `sdk.codex_sdk` | `openai-codex` | `agent.model` | `max_steps` is unused |
| `sdk.claude_sdk` | `claude-agent-sdk` | `agent.model` | `max_turns` per phase |

```shell
uv run nika agent run -a sdk.codex_sdk -m gpt-5-mini -e medium
uv run nika agent run -a sdk.claude_sdk -p deepseek -m deepseek-v4-flash -n 20
```

SDK sandboxes install dependencies from PyPI unless you enable `nika.sandbox.offline_sdk_wheels`. See [SDK offline wheels](../operations/agent-sandbox.md#sdk-agents-optional-offline-wheels).

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
