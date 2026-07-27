# Agent Architecture

`src/agent` hosts multiple troubleshooting agent implementations for NIKA. All share the same entry contract (`protocols.TroubleshootingAgent`) and produce the same session artifacts (`messages.jsonl`, `submission.json`, etc.).

## Directory Layout

```
src/agent/
├── protocols.py          # Shared Protocol interface
├── registry.py           # Type registry and factory for `nika agent run`
├── byo/                  # Bring-your-own LLM / agent framework backends
│   ├── langgraph/        # -a byo.langgraph (LangChain ReAct workers)
│   │   ├── react_agent.py
│   │   └── phases/
│   ├── mcp_agent/        # -a byo.mcp_agent
│   └── autogen/          # -a byo.autogen
├── local_cli/            # Local CLI subprocess workers
│   ├── codex_cli/        # -a local_cli.codex_cli
│   └── claude_cli/       # -a local_cli.claude_cli
├── community/            # Community-contributed agents
│   └── sade/             # -a community.sade
├── mock/                 # Test-only deterministic agent (see tests/README.md)
│   └── mock_agent.py
├── sdk/                  # SDK agents (claude-agent-sdk, openai-codex)
│   ├── claude_sdk/       # -a sdk.claude_sdk
│   └── codex_sdk/        # -a sdk.codex_sdk
├── sandbox/              # Docker Sandboxes (sbx) runner / manager / credentials
├── skills/               # Shared skill library (.claude/ + .agents/)
├── llm/                  # LangChain model factory (langgraph path)
└── utils/                # MCP config, phases, loggers, skills helpers
```

## Agent Types

| CLI name | Orchestration | LLM access | Status |
|----------|---------------|------------|--------|
| `byo.langgraph` | LangGraph `StateGraph` | LangChain ReAct + `load_model()` | Implemented |
| `local_cli.codex_cli` | Native two-phase (no LangGraph) | `codex exec` subprocess + shared `.agents/skills/` | Implemented |
| `local_cli.claude_cli` | Native two-phase (no LangGraph) | `claude -p` subprocess + shared `.claude/skills/` | Implemented |
| `byo.mcp_agent` | mcp-agent `Workflow` | mcp-agent + OpenAI | Implemented |
| `byo.autogen` | AutoGen `GraphFlow` | AutoGen AgentChat + OpenAI | Implemented |
| `community.sade` | Single Claude Code session + 15-skill library | `claude-agent-sdk` (optional extra `sade`) | Implemented |
| `sdk.claude_sdk` | Native two-phase `ClaudeSDKClient` sessions | `claude-agent-sdk` + shared skills (optional extra `sdk`) | Implemented |
| `sdk.codex_sdk` | Native two-phase `AsyncCodex` threads | `openai-codex` + shared skills (optional extra `sdk`) | Implemented |

## Community Agents

Community-contributed agents live under `src/agent/community/<name>/` and implement the
same `protocols.TroubleshootingAgent` contract.

See [`community/sade/README.md`](community/sade/README.md) for SADE setup, DeepSeek
credentials, and the paper citation (arXiv:2605.04530).

## Agent Skills

Claude Code and Codex agents load the shared skill library from `src/agent/skills/` when
`NIKA_ENABLE_SKILLS=true` (default). Helpers live in `agent.utils.skills`.

See **[docs/agent-skills.md](../../docs/agent-skills.md)** for authoring custom skills.
Integration tests: `tests/agent/test_skills.py`.

## Shared Pipeline

Every agent runs **diagnosis** (Kathara MCP, `if_submit=False`) then **submission** (task MCP, `if_submit=True` → `list_avail_problems` + `submit`).

## CLI & Environment

`nika agent run` resolves options from CLI flags first, then `.env`. See [`.env.example`](../../.env.example) for a full template.

### Shared (all agents)

| Flag | Env | Required | Notes |
|------|-----|----------|-------|
| `-a` / `--agent` | `NIKA_AGENT_TYPE` | Yes | `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `local_cli.codex_cli`, `local_cli.claude_cli`, `community.sade`, `sdk.claude_sdk`, `sdk.codex_sdk` |
| `-p` / `--provider` | `NIKA_LLM_PROVIDER` | byo.langgraph only | `openai`, `ollama`, `deepseek`, `custom` |
| `-n` / `--max-steps` | `NIKA_MAX_STEPS` | Yes | Limits steps per phase in `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, and `sdk.claude_sdk` |
| `-m` / `--model` | `NIKA_MODEL` | No | Overrides agent-specific model env when set |
| `--session_id` | — | No | Target session (default: current running session) |

### Sandbox (non-BYO agents)

CLI, SDK, and SADE agents always run inside Docker Sandboxes (`sbx` microVMs) using official `codex` / `claude` / `shell` templates. MCP tools and the network lab stay on the host. BYO agents run on the host. See **[docs/agent-sandbox.md](../../docs/agent-sandbox.md)**.

Auth uses the host `sbx secret` store (credential proxy). API keys in `.env` are synced automatically; Codex subscription uses `sbx secret set -g openai --oauth`; Claude subscription uses `/login`. Host auth files are never copied into the sandbox.

| Flag | Env | Notes |
|------|-----|-------|
| `--sandbox-env-file` | `NIKA_SANDBOX_ENV_FILE` | Credential resolution (default repo `.env`) |
| `--sandbox-keep-container` | `NIKA_SANDBOX_KEEP` | Keep the sandbox after agent exit (debug) |
| `--sandbox-cpus` | `NIKA_SANDBOX_CPUS` | sbx CPU limit |
| `--sandbox-memory` | `NIKA_SANDBOX_MEMORY` | sbx memory limit |
| `--sandbox-offline-sdk-wheels` | `NIKA_SANDBOX_OFFLINE_SDK_WHEELS` | Optional; speeds up SDK/SADE deploys via host-cached wheels |
| `--sandbox-proxy` | `NIKA_SANDBOX_UPSTREAM_PROXY` | Optional upstream proxy for sbx daemon |

Outbound proxy and offline SDK wheels are **off by default**. Enable via repo-root `.env` when needed (see `.env.example`).

```bash
uv run nika agent run -a local_cli.codex_cli -m gpt-5-mini -n 20
uv run nika agent run -a local_cli.claude_cli -m deepseek-v4-flash -n 20
```

Model resolution order: `-m` → `NIKA_MODEL` → agent-specific env (below).

### Observability (byo.langgraph)

Langfuse is optional and imported only when `NIKA_LANGFUSE_ENABLED=true`.

Install with `uv sync --extra observability`, then configure `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, and `LANGFUSE_HOST`.

---

## byo.langgraph

LangGraph orchestration + LangChain ReAct workers per phase.

**Entry**: `agent.byo.langgraph.react_agent.BasicReActAgent`

**Requires**: API key for the chosen provider.

| Provider | API key / URL |
|----------|---------------|
| `openai` | `OPENAI_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `ollama` | `OLLAMA_API_URL` (default `http://localhost:11434`) |
| `custom` | `CUSTOM_API_BASE`, optional `CUSTOM_API_KEY` |

| Env | Default in `.env.example` |
|-----|-------------------------|
| `NIKA_LANGGRAPH_MODEL` | `gpt-5-mini` |

```bash
# .env
NIKA_AGENT_TYPE=byo.langgraph
NIKA_LLM_PROVIDER=openai
NIKA_MAX_STEPS=20
NIKA_LANGGRAPH_MODEL=gpt-5-mini
OPENAI_API_KEY=sk-...

nika agent run                              # all from .env
nika agent run -a byo.langgraph -p deepseek -m deepseek-chat -n 20
```

### Local deployment (Ollama)

Requires a tool-calling model — see [Ollama tool calling](https://github.com/ollama/ollama/blob/main/docs/capabilities/tool-calling.mdx). Install, pull, and server setup: [Ollama FAQ](https://docs.ollama.com/faq).

Common small models: `qwen2.5:7b`, `llama3.2:3b`, `llama3.1:8b`.

```bash
# .env
NIKA_AGENT_TYPE=byo.langgraph
NIKA_LLM_PROVIDER=ollama
NIKA_MAX_STEPS=20
NIKA_LANGGRAPH_MODEL=qwen2.5:7b
OLLAMA_API_URL=http://localhost:11434

nika agent run -a byo.langgraph -p ollama -m qwen2.5:7b -n 20
```

No API key. `load_model()` validates the model at init — run `ollama pull` first. For a remote host, set `OLLAMA_API_URL` to the server base URL.

---

## local_cli.codex_cli

Native two-phase orchestration + `codex exec` via `sbx exec` (native `codex` template). Ephemeral workspace under `.sandbox_run/` (discarded after the run). MCP config written to an isolated `CODEX_HOME`.

**Entry**: `agent.local_cli.codex_cli.agent.CodexCliAgent`

**Requires**: [Codex CLI](https://github.com/openai/codex) available in the sbx `codex` template. Auth: `OPENAI_API_KEY` in `.env` (synced to `sbx secret`) or `sbx secret set -g openai --oauth`.

| Flag | Env | Notes |
|------|-----|-------|
| `-m` / `--model` | `NIKA_CODEX_MODEL` | Default `gpt-5.4-mini` |
| `-e` / `--reasoning-effort` | `NIKA_CODEX_REASONING_EFFORT` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`; optional |

```bash
# .env
NIKA_AGENT_TYPE=local_cli.codex_cli
NIKA_MAX_STEPS=20
NIKA_CODEX_MODEL=gpt-5-mini
# NIKA_CODEX_REASONING_EFFORT=medium
# or: sbx secret set -g openai --oauth

nika agent run -a local_cli.codex_cli -m gpt-5-mini -e medium
```

---

## local_cli.claude_cli

Native two-phase orchestration + `claude -p` via `sbx exec` (native `claude` template). Ephemeral workspace under `.sandbox_run/` (discarded after the run). MCP config: `{phase}_mcp_config.json`.

**Entry**: `agent.local_cli.claude_cli.agent.ClaudeAgent`

**Requires**: Claude Code available in the sbx `claude` template.

**Auth** (pick one):

| Mode | Setup |
|------|-------|
| DeepSeek (preferred) | `DEEPSEEK_API_KEY` (defaults `ANTHROPIC_BASE_URL` to DeepSeek Anthropic path) |
| Compatible proxy | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` |
| Native Anthropic API | `ANTHROPIC_API_KEY` in `.env` (auto-synced to `sbx secret`) |
| Claude subscription | `/login` so the host stores the `anthropic` sbx secret |

When credentials come from env vars, NIKA runs `claude` with `--bare`. Subscription / OAuth mode does not use `--bare`.

**Model** (when `-m` omitted, first non-empty wins):

1. `ANTHROPIC_MODEL`
2. `CLAUDE_CODE_SUBAGENT_MODEL`
3. `ANTHROPIC_DEFAULT_SONNET_MODEL`

If none are set, pass `-m` or configure `.env`.

```bash
# .env — DeepSeek via Anthropic-compatible API
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN=sk-...
ANTHROPIC_MODEL=deepseek-v4-pro[1m]

NIKA_AGENT_TYPE=local_cli.claude_cli
NIKA_MAX_STEPS=20

nika agent run -a local_cli.claude_cli
nika agent run -a local_cli.claude_cli -m deepseek-v4-flash
```

---

## byo.mcp_agent

mcp-agent ``Workflow`` orchestration + [mcp-agent SDK](https://docs.mcp-agent.com/mcp-agent-sdk/overview) workers per phase.

**Entry**: `agent.byo.mcp_agent.agent.McpAgent`

**Requires**: `OPENAI_API_KEY`.

| Env | Default in `.env.example` |
|-----|-------------------------|
| `NIKA_MCP_AGENT_MODEL` | `gpt-4.1-mini` (use `gpt-4o-mini` if unavailable) |

```bash
# .env
NIKA_AGENT_TYPE=byo.mcp_agent
NIKA_MAX_STEPS=20
NIKA_MCP_AGENT_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-...

nika agent run -a byo.mcp_agent -m gpt-4.1-mini -n 20
```

No Langfuse integration in this path (observability deferred).

---

## byo.autogen

AutoGen ``GraphFlow`` orchestration + [AutoGen AgentChat](https://microsoft.github.io/autogen/stable/) workers per phase.

**Entry**: `agent.byo.autogen.agent.AutogenAgent`

**Requires**: `OPENAI_API_KEY` for the default model. When `-m` / `NIKA_AUTOGEN_MODEL` starts with `deepseek`, uses `DEEPSEEK_API_KEY` instead.

| Env | Default in `.env.example` |
|-----|-------------------------|
| `NIKA_AUTOGEN_MODEL` | `gpt-4.1-mini` (use `gpt-4o-mini` if unavailable) |

```bash
# .env
NIKA_AGENT_TYPE=byo.autogen
NIKA_MAX_STEPS=20
NIKA_AUTOGEN_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-...

nika agent run -a byo.autogen -m gpt-4.1-mini -n 20
```

No Langfuse integration in this path (observability deferred).

---

## sdk.claude_sdk

Native two-phase pipeline via ``claude-agent-sdk`` ``ClaudeSDKClient`` (no LangGraph). Each phase starts a separate SDK session with phase-specific MCP servers.

**Entry**: `agent.sdk.claude_sdk.agent.ClaudeSdkAgent`

**Requires**: `uv sync --extra sdk --prerelease=allow`

**Auth**: Anthropic API key / token in `.env` (auto-synced), or Claude subscription via `/login` (`anthropic` sbx secret). Same modes as `local_cli.claude_cli`.

```bash
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN=sk-...
ANTHROPIC_MODEL=deepseek-v4-pro[1m]
```

| Flag | Env | Notes |
|------|-----|-------|
| `-n` / `--max-steps` | `NIKA_MAX_STEPS` | Passed to SDK `max_turns` per phase |
| `-m` / `--model` | `NIKA_CLAUDE_SDK_MODEL` or `ANTHROPIC_MODEL` chain | |

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

| Flag | Env | Notes |
|------|-----|-------|
| `-m` / `--model` | `NIKA_CODEX_SDK_MODEL` or `NIKA_CODEX_MODEL` | Default `gpt-5.4-mini` |
| `-e` / `--reasoning-effort` | `NIKA_CODEX_REASONING_EFFORT` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |

```bash
# API key in .env, or once:
# sbx secret set -g openai --oauth

nika agent run -a sdk.codex_sdk -m gpt-5.4-mini -e medium
```

---

## Example Workflow

```bash
nika env run simple_bgp
nika failure inject link_down --set host_name=pc1 --set intf_name=eth0
nika agent run -a local_cli.codex_cli -m gpt-5.4-mini
nika session close -y
nika eval metrics
```

See the root [README.md](../../README.md#troubleshooting-agents) for a longer walkthrough.

## Adding a New Agent

Place each agent in its own package under `src/agent/`:

```text
src/agent/community/my_agent/
├── __init__.py
├── agent.py
└── (other files)
```

Implement `agent.protocols.TroubleshootingAgent` (`session_id` +
`async def run(task_description) -> dict`), register the id in
`agent.registry.create_agent()`, write traces to `{session_dir}/messages.jsonl`,
and call the task MCP `submit` tool before returning. Sandbox agents also need
their id in `SANDBOX_AGENT_TYPES`.

Implementation example, registration, and checklist:
[docs/custom-agents.md](../../docs/custom-agents.md).

## CLI Reference

```bash
nika agent list          # agent types, LLM providers, reasoning-effort levels
nika agent run [options] # dispatch via nika/workflows/agent/run.py → registry.create_agent()
```
