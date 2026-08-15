# Agent implementation reference

This reference is for maintainers of NIKA's built-in troubleshooting agents. Each implementation follows `protocols.TroubleshootingAgent` and produces the same session artifacts, including `messages.jsonl` and `submission.json`.

Implementation: [`protocols.py`](../src/agent/protocols.py) defines the agent contract, and [`registry.py`](../src/agent/registry.py) maps CLI names to agent classes.

## Directory layout

```
src/agent/
├── protocols.py          # Agent contract and diagnosis/submission phase ids
├── registry.py           # Type registry and factory for `nika agent run`
├── byo/                  # Bring-your-own LLM / agent framework backends
│   ├── langgraph/        # -a byo.langgraph (LangChain ReAct workers)
│   │   ├── react_agent.py
│   │   └── phases/
│   ├── mcp_agent/        # -a byo.mcp_agent
│   └── autogen/          # -a byo.autogen
├── cli/                  # Official Codex / Claude CLI agents (sbx)
│   ├── codex/            # -a cli.codex
│   └── claude/           # -a cli.claude
├── community/            # Community-contributed agents
│   └── sade/             # -a community.sade
├── mock/                 # Test-only deterministic agent (see docs/testing.md)
│   └── mock_agent.py
├── sdk/                  # SDK agents (claude-agent-sdk, openai-codex)
│   ├── claude_sdk/       # -a sdk.claude_sdk
│   └── codex_sdk/        # -a sdk.codex_sdk
├── sandbox/              # Docker Sandboxes (sbx) runner / manager / credentials
├── skills/               # Shared skill library (.claude/ + .agents/)
├── llm/                  # LangChain model factory (langgraph path)
└── utils/                # MCP config, loggers, skills helpers
```

## Agent types

| CLI name | Orchestration | LLM access |
|----------|---------------|------------|
| `byo.langgraph` | LangGraph `StateGraph` | LangChain ReAct + `load_model()` |
| `cli.codex` | Native two-phase | `codex exec` subprocess + shared `.agents/skills/` |
| `cli.claude` | Native two-phase | `claude -p` subprocess + shared `.claude/skills/` |
| `byo.mcp_agent` | mcp-agent `Workflow` | mcp-agent + OpenAI / Anthropic |
| `byo.autogen` | AutoGen `GraphFlow` | AutoGen AgentChat + OpenAI / Anthropic |
| `community.sade` | Single Claude Code session + 15-skill library | `claude-agent-sdk` (optional extra `sade`) |
| `sdk.claude_sdk` | Native two-phase `ClaudeSDKClient` sessions | `claude-agent-sdk` + shared skills (optional extra `sdk`) |
| `sdk.codex_sdk` | Native two-phase `AsyncCodex` threads | `openai-codex` + shared skills (optional extra `sdk`) |

## Community agents

Community-contributed agents live under `src/agent/community/<name>/` and implement the same `protocols.TroubleshootingAgent` contract.

See the [SADE community agent reference](agents/community/sade.md) for setup, DeepSeek credentials, and the paper citation (arXiv:2605.04530).

## Agent skills

Claude Code and Codex agents load the shared skill library from `src/agent/skills/` when `nika.enable_skills: true` in `config/nika.yaml` (the default). Helpers live in `agent.utils.skills`. The integration-only `nika-test-skill` is under `test_skills/` and is loaded only when tests pass `include_test_skill=True` to the prepare/prompt helpers.

See [Configure agent skills](agent-skills.md) to author a custom skill. Integration tests: `tests/agent/test_skills.py`.

## Shared pipeline

Every agent runs **diagnosis** (Kathara MCP, `if_submit=False`) then **submission** (task MCP, `if_submit=True` → `list_avail_problems` + `submit`).

## CLI and environment

`nika agent run` resolves options from CLI flags first, then the local `config/nika.yaml` (copy the tracked [`config/nika.example.yaml`](../config/nika.example.yaml)). Credentials stay in [`.env`](../.env.example). See `nika config show` or `nika config migrate`.

### Shared (all agents)

| Flag | Config / Env | Required | Notes |
|------|--------------|----------|-------|
| `-a` / `--agent` | `agent.type` | Yes | via YAML or flag |
| `-p` / `--provider` | `agent.provider` | Yes | `openai`, `anthropic`, `deepseek`, `custom` |
| `-n` / `--max-steps` | `agent.max_steps` | Yes | |
| `-m` / `--model` | `agent.model` / `agent.models.*` | No | |
| `--run-config` | `NIKA_RUN_CONFIG` | No | path to YAML (default `config/nika.yaml`) |
| `--session_id` | Not set | No | Target session |

Provider credentials live in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, or `NIKA_CUSTOM_API_KEY`). Custom base URL/model live under `agent.custom` in YAML.

### Sandbox (non-BYO agents)

CLI, SDK, and SADE agents run inside Docker Sandboxes (`sbx` microVMs) using the `codex`, `claude`, or `shell` templates. MCP tools and the network lab stay on the host. BYO agents run on the host. See the [agent sandbox guide](agent-sandbox.md).

Authentication uses the host `sbx secret` store. NIKA syncs API keys from `.env`; Codex subscriptions use `sbx secret set -g openai --oauth`, and Claude subscriptions use `/login`. NIKA does not copy host authentication files into the sandbox.

| Flag / config | Notes |
|---------------|-------|
| `--sandbox-keep-container` | Keep the sandbox after agent exit (debug) |
| `--sandbox-cpus` / `--sandbox-memory` | sbx resource limits |
| `--sandbox-offline-sdk-wheels` | Optional; host-cached wheels |
| `--sandbox-proxy` | Optional upstream proxy |
| `nika.sandbox.*` | Defaults in `config/nika.yaml` |

Outbound proxy and offline SDK wheels are **off by default**. Enable in `config/nika.yaml` when needed.

```bash
uv run nika agent run -a cli.codex -m gpt-5-mini -n 20
uv run nika agent run -a cli.claude -p deepseek -m deepseek-v4-flash -n 20
```

Model resolution order: `-m` → `agent.model` → `agent.models.<agent>` → `agent.custom.model` when provider is `custom`.

### Observability (byo.langgraph)

Langfuse is optional and imported only when `nika.observability.langfuse_enabled` is true in YAML.

Install with `uv sync --extra observability`, then set `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` in `.env` (host optional via `nika.observability.langfuse_host`).

---

## byo.langgraph

LangGraph orchestration + LangChain ReAct workers per phase.

**Entry**: `agent.byo.langgraph.react_agent.BasicReActAgent`

**Requires**: API key for the chosen provider.

| Provider | Credentials / URL |
|----------|-------------------|
| `openai` | `OPENAI_API_KEY` in `.env` |
| `anthropic` | `ANTHROPIC_API_KEY` in `.env`; optional `ANTHROPIC_BASE_URL` for Anthropic-compatible endpoints |
| `deepseek` | `DEEPSEEK_API_KEY` in `.env` |
| `custom` | `agent.custom.base_url` (+ optional model) in YAML; `NIKA_CUSTOM_API_KEY` in `.env` if needed |

```yaml
# config/nika.yaml
agent:
  type: byo.langgraph
  provider: openai
  max_steps: 20
  models:
    langgraph: gpt-5-mini
```

```bash
nika agent run                              # from config/nika.yaml + .env
nika agent run -a byo.langgraph -p deepseek -m deepseek-chat -n 20
nika agent run -a byo.langgraph -p anthropic -m claude-haiku-4-5 -n 20
```

### Local / OpenAI-compatible endpoints (`custom`)

Use `-p custom` for any OpenAI-compatible server (Ollama, vLLM, etc.).

```yaml
# config/nika.yaml
agent:
  type: byo.langgraph
  provider: custom
  max_steps: 20
  models:
    langgraph: qwen2.5:7b
  custom:
    base_url: http://localhost:11434/v1
    # model: optional default when -m / models.* omitted
```

```bash
# Optional key in .env when the server requires auth:
# NIKA_CUSTOM_API_KEY=...

nika agent run -a byo.langgraph -p custom -m qwen2.5:7b -n 20
```

---

## cli.codex

Native two-phase orchestration + `codex exec` via `sbx exec` (native `codex` template). Ephemeral workspace under `.sandbox_run/` (discarded after the run). MCP config written to an isolated `CODEX_HOME`.

**Entry**: `agent.cli.codex.agent.CodexCliAgent`

**Requires**: [Codex CLI](https://github.com/openai/codex) available in the sbx `codex` template. Auth: `OPENAI_API_KEY` in `.env` (synced to `sbx secret`) or `sbx secret set -g openai --oauth`.

| Flag | YAML | Notes |
|------|------|-------|
| `-m` / `--model` | `agent.models.codex` | Default `gpt-5.4-mini` |
| `-e` / `--reasoning-effort` | `agent.reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`; optional |

```yaml
# config/nika.yaml
agent:
  type: cli.codex
  provider: openai
  max_steps: 20
  models:
    codex: gpt-5-mini
```

```bash
# OPENAI_API_KEY in .env, or: sbx secret set -g openai --oauth
nika agent run -a cli.codex -m gpt-5-mini -e medium
```

---

## cli.claude

Native two-phase orchestration + `claude -p` via `sbx exec` (native `claude` template). Ephemeral workspace under `.sandbox_run/` (discarded after the run). MCP config: `{phase}_mcp_config.json`.

**Entry**: `agent.cli.claude.agent.ClaudeAgent`

**Requires**: Claude Code available in the sbx `claude` template.

**Auth** (pick one):

| Mode | Setup |
|------|-------|
| DeepSeek (preferred) | `DEEPSEEK_API_KEY` in `.env` + `agent.provider: deepseek` |
| Native Anthropic API | `ANTHROPIC_API_KEY` in `.env` + `agent.provider: anthropic` |
| Custom proxy | `agent.custom.base_url` (+ optional `NIKA_CUSTOM_API_KEY`) + `agent.provider: custom` |
| Claude subscription | `/login` so the host stores the `anthropic` sbx secret |

When credentials come from env vars, NIKA runs `claude` with `--bare`. Subscription / OAuth mode does not use `--bare`.

**Model** (when `-m` omitted): `agent.models.claude`, then Anthropic/Claude Code default model env vars if present.

```yaml
agent:
  type: cli.claude
  provider: deepseek
  max_steps: 20
  models:
    claude: deepseek-v4-pro[1m]
```

```bash
nika agent run -a cli.claude
nika agent run -a cli.claude -m deepseek-v4-flash
```

---

## byo.mcp_agent

mcp-agent ``Workflow`` orchestration + [mcp-agent SDK](https://docs.mcp-agent.com/mcp-agent-sdk/overview) workers per phase.

**Entry**: `agent.byo.mcp_agent.agent.McpAgent`

**Requires**: API key for the chosen provider.

| Provider | Credentials / URL |
|----------|-------------------|
| `openai` | `OPENAI_API_KEY` in `.env` |
| `anthropic` | `ANTHROPIC_API_KEY` in `.env`; optional `ANTHROPIC_BASE_URL` for Anthropic-compatible endpoints |
| `deepseek` | `DEEPSEEK_API_KEY` in `.env` |
| `custom` | `agent.custom.base_url` (+ optional model) in YAML; `NIKA_CUSTOM_API_KEY` in `.env` if needed |

```yaml
agent:
  type: byo.mcp_agent
  provider: openai
  max_steps: 20
  models:
    mcp_agent: gpt-4.1-mini
```

```bash
nika agent run -a byo.mcp_agent -m gpt-4.1-mini -n 20
nika agent run -a byo.mcp_agent -p anthropic -m claude-haiku-4-5 -n 20
```

---

## byo.autogen

AutoGen ``GraphFlow`` orchestration + [AutoGen AgentChat](https://microsoft.github.io/autogen/stable/) workers per phase.

**Entry**: `agent.byo.autogen.agent.AutogenAgent`

**Requires**: API key for the chosen provider.

| Provider | Credentials / URL |
|----------|-------------------|
| `openai` | `OPENAI_API_KEY` in `.env` |
| `anthropic` | `ANTHROPIC_API_KEY` in `.env`; optional `ANTHROPIC_BASE_URL` for Anthropic-compatible endpoints |
| `deepseek` | `DEEPSEEK_API_KEY` in `.env` |
| `custom` | `agent.custom.base_url` (+ optional model) in YAML; `NIKA_CUSTOM_API_KEY` in `.env` if needed |

```yaml
agent:
  type: byo.autogen
  provider: openai
  max_steps: 20
  models:
    autogen: gpt-4.1-mini
```

```bash
nika agent run -a byo.autogen -m gpt-4.1-mini -n 20
nika agent run -a byo.autogen -p anthropic -m claude-haiku-4-5 -n 20
```

---

## sdk.claude_sdk

Native two-phase pipeline via ``claude-agent-sdk`` ``ClaudeSDKClient`` (no LangGraph). Each phase starts a separate SDK session with phase-specific MCP servers.

**Entry**: `agent.sdk.claude_sdk.agent.ClaudeSdkAgent`

**Requires**: `uv sync --extra sdk --prerelease=allow`

**Auth**: Same as `cli.claude`.

```yaml
agent:
  provider: deepseek
  models:
    claude: deepseek-v4-pro[1m]
```

| Flag | YAML | Notes |
|------|------|-------|
| `-n` / `--max-steps` | `agent.max_steps` | Passed to SDK `max_turns` per phase |
| `-m` / `--model` | `agent.models.claude_sdk` / `claude` | |

```bash
nika agent run -a sdk.claude_sdk -n 20
nika agent run -a sdk.claude_sdk -m deepseek-v4-flash
```

---

## sdk.codex_sdk

Native two-phase pipeline via ``openai-codex`` ``AsyncCodex`` threads (no LangGraph). MCP config is written to an isolated `CODEX_HOME` per phase.

**Entry**: `agent.sdk.codex_sdk.agent.CodexSdkAgent`

**Requires**: `uv sync --extra sdk --prerelease=allow`

**Auth**: `OPENAI_API_KEY` in `.env` (auto-synced to `sbx secret`) or Codex subscription via `sbx secret set -g openai --oauth`.

| Flag | YAML | Notes |
|------|------|-------|
| `-m` / `--model` | `agent.models.codex_sdk` / `codex` | Default `gpt-5.4-mini` |
| `-e` / `--reasoning-effort` | `agent.reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |

```bash
nika agent run -a sdk.codex_sdk -m gpt-5-mini -e medium
```

---

## Add a new agent

Place each agent in its own package under `src/agent/`:

```text
src/agent/community/my_agent/
├── __init__.py
├── agent.py
└── (other files)
```

Implement `agent.protocols.TroubleshootingAgent` (`session_id` + `async def run(task_description) -> dict`), register the id in `agent.registry.create_agent()`, write traces to `{session_dir}/messages.jsonl`, and call the task MCP `submit` tool before returning. Sandbox agents also need their id in `SANDBOX_AGENT_TYPES`.

Implementation example, registration, and checklist: [custom agent integration guide](custom-agents.md).

## CLI reference

```bash
nika agent list          # agent types, LLM providers, reasoning-effort levels
nika agent run [options] # dispatch via nika/workflows/agent/run.py → registry.create_agent()
```
