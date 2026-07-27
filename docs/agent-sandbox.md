# Agent Sandbox Execution

NIKA runs every non-BYO production agent inside **[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)** (`sbx` microVMs). The network lab, MCP gateway, and orchestration stay on the host; only the LLM agent process runs in the microVM. `byo.*` agents remain host-executed.

## How it works

NIKA uses **native sbx agent templates** (`codex`, `claude`, `shell`) from Docker Sandboxes.

| Concern | Behavior |
|---------|----------|
| Isolation | per-session microVM + workspace; MCP gateway on ephemeral port; sbx policy blocks peer ports |
| Agent binaries | Official `codex` / `claude` / `shell` sandbox templates |
| Credentials | Host `sbx secret` store; microVM sees placeholders (`proxy-managed` / custom placeholders) only |
| Network policy | `sbx policy allow network` (MCP gateway + LLM hosts such as `api.deepseek.com`) |
| Orchestration | Host two-phase / SDK driver; agent turn via `sbx exec` |
| Lab / tools | Host MCP HTTP gateway only (no lab binaries inside the VM) |

## Supported agents

| NIKA agent | Native sbx agent | Execution model |
|------------|------------------|-----------------|
| `local_cli.codex_cli` | `codex` | Host two-phase driver → `sbx exec codex exec ...` |
| `local_cli.claude_cli` | `claude` | Host two-phase driver → `sbx exec claude -p ...` |
| `sdk.codex_sdk` | `shell` (+ optional offline SDK wheels) | In-sandbox Python runner via `sbx exec` |
| `sdk.claude_sdk` | `shell` (+ optional offline SDK wheels) | In-sandbox Python runner via `sbx exec` |
| `community.sade` | `shell` (+ optional offline SDK wheels) | In-sandbox Python runner via `sbx exec` |

## Prerequisites

- `sbx` CLI installed and logged in (`sbx login`)
- KVM available on Linux
- Docker for Kathara / Containerlab labs
- Credentials for the agent you run (see [Authentication](#authentication))

## Quick start

```bash
uv run nika env run simple_bgp
uv run nika failure inject link_down --device pc1 --interface eth0
uv run nika agent run -a local_cli.codex_cli -m gpt-5-mini -n 20
# or Claude via DeepSeek:
# uv run nika agent run -a local_cli.claude_cli -m deepseek-v4-flash -n 20
uv run nika session close
uv run nika eval
```

## Architecture

```
Host (NIKA orchestration)          sbx microVM (agent-only)
├── Kathara / MCP gateway          ├── codex exec / claude -p  (CLI agents)
├── ground_truth.json              └── Python SDK runner         (SDK / SADE)
├── Host phase / SDK driver            workspace = .sandbox_run/
└── sbx create / exec / policy         bundled: agent/ + skills (+ optional wheels)
                                       lab interaction: MCP HTTP only
```

Each task uses an ephemeral `results/{session_id}/.sandbox_run/` workspace (manifest + skills). `ground_truth.json` stays on the host and is never mounted into the microVM. After the run, only standardized session artifacts (`messages.jsonl`, `submission.json`, plus `sandbox_manifest.json`) are copied back; `.sandbox_run` and agent CLI/SDK workspaces are discarded.

**Concurrent isolation:** each agent run gets its own sbx microVM (`nika-{session_id}`), workspace, and host MCP gateway on an ephemeral port. The sandbox network policy allows only that session’s `localhost:{port}`; peer gateway ports are blocked. Parallel benchmark batches (`--batch-size N`) run one subprocess per case so gateways never share a process.

**Sandbox boundary:** SDK sandboxes do **not** bundle `nika/` source. The host writes MCP HTTP endpoints into `sandbox_manifest.json` (`mcp_servers`); the in-sandbox runner loads agent code, prompts/skills, and (when enabled) SDK wheels only.

### SDK agents (optional offline wheels)

`sdk.*` and SADE create a `shell` sandbox, then install Python deps after `sbx create`. Offline wheels are **off by default**; without them, packages install from PyPI inside the microVM.

Enable offline wheels to speed up SDK/SADE sandbox deploys and avoid re-downloading / reconfiguring deps on every `sbx` start. Host-staged wheels (`.sdk_wheels/`, cached under `.nika_cache/sbx-sdk-wheels/`) are installed with `pip --no-index`, which is also more reliable than slow in-VM PyPI installs that can OOM.

Package versions are **frozen** in [`src/agent/sandbox/sbx/requirements-sdk.txt`](../src/agent/sandbox/sbx/requirements-sdk.txt) (direct + transitive pins). Changing that file invalidates the wheel cache. Prefer editing the freeze intentionally over floating latest PyPI releases.

```bash
# .env
NIKA_SANDBOX_OFFLINE_SDK_WHEELS=true

# or CLI
uv run nika agent run -a sdk.claude_sdk --sandbox-offline-sdk-wheels ...
```

### Authentication

Credentials follow Docker Sandboxes [credential isolation](https://docs.docker.com/ai/sandboxes/security/credentials/): the real secret stays on the host; the sandbox only sees a sentinel or placeholder. **NIKA never copies** `~/.codex/auth.json` or `~/.claude/.credentials.json` into the workspace.

#### API keys (automatic)

Put keys in the repo-root `.env`. Before `sbx create`, NIKA syncs them into sbx secrets.

```bash
# Codex — OpenAI
OPENAI_API_KEY=sk-...

# Claude / SADE — Anthropic-compatible (DeepSeek preferred)
DEEPSEEK_API_KEY=sk-...
# ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
# ANTHROPIC_AUTH_TOKEN=...   # alternative to DEEPSEEK_API_KEY for Claude
```

| Agent | Credential path |
|-------|-----------------|
| Codex CLI / SDK | Built-in `openai` sbx secret from `OPENAI_API_KEY` (or OAuth below) |
| Claude CLI / SDK / SADE | `sbx secret set-custom` for `api.deepseek.com` + Anthropic-compatible URL |

#### Subscription / OAuth (interactive, once)

| Provider | User action |
|----------|-------------|
| Codex / ChatGPT | `sbx secret set -g openai --oauth` |
| Claude | `/login` inside Claude Code so the host stores the `anthropic` secret ([docs](https://docs.docker.com/ai/sandboxes/agents/claude-code/)) |

Confirm with `sbx secret ls`. Global secrets apply when a sandbox is created; recreate the sandbox after changing secrets.

NIKA also allows `api.deepseek.com` in the sbx network policy for API-key runs.

#### Outbound proxy (Clash / TUN)

Optional. Off by default.

With Clash **TUN** enabled, sandbox microVMs can often reach LLM APIs with no
extra NIKA setting. If OpenAI (or another API) still fails inside the sandbox,
point sbx at Clash's HTTP mixed port:

```bash
# .env
NIKA_SANDBOX_UPSTREAM_PROXY=http://127.0.0.1:7890
```

Or pass `--sandbox-proxy http://127.0.0.1:7890`. NIKA reloads the sbx daemon
with `DOCKER_SANDBOXES_PROXY` as the current user (once per proxy URL).
DeepSeek often works with neither TUN nor this setting.

## Configuration

| Flag / env | Description |
|------------|-------------|
| `--sandbox-env-file` / `NIKA_SANDBOX_ENV_FILE` | Env file for credential sync (default repo `.env`) |
| `--sandbox-proxy` / `NIKA_SANDBOX_UPSTREAM_PROXY` | Upstream proxy for sbx daemon |
| `--sandbox-offline-sdk-wheels` / `NIKA_SANDBOX_OFFLINE_SDK_WHEELS` | Cache & install SDK deps from host wheels (off by default; speeds up SDK/SADE deploys) |
| `--sandbox-keep-container` / `NIKA_SANDBOX_KEEP` | Keep the sandbox after exit (debug) |
| `--sandbox-cpus` / `NIKA_SANDBOX_CPUS` | CPU limit |
| `--sandbox-memory` / `NIKA_SANDBOX_MEMORY` | Memory limit (e.g. `8g`) |

## Testing

```bash
# Unit (no sbx / lab)
uv run pytest tests/agent/test_sbx.py tests/agent/test_sandbox.py -v

# Security probe (sbx + MCP gateway)
uv run pytest tests/agent/test_sandbox_security.py -v

# Cross-sandbox MCP port isolation (unit + sbx peer-gateway probe)
uv run pytest tests/agent/test_sandbox_isolation.py -v

# E2E — five sandbox agents (Codex needs OPENAI_API_KEY; Claude/SADE need DEEPSEEK_API_KEY)
uv run pytest tests/agent/test_sandbox_agents.py -v

# Benchmark uses the same sandbox path as `nika agent run`
# (includes Claude + Codex parallel --batch-size 2 cases)
uv run pytest tests/benchmark/test_sandbox_benchmark.py -v
```

Proxy settings come from repo-root `.env` or `--sandbox-proxy` (off by default). For sandbox test commands and verification status, see [tests/README.md](../tests/README.md).
