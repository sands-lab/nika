# Run agents in Docker Sandboxes

This guide is for operators who run NIKA's CLI, SDK, or SADE agents in **[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)** (`sbx` microVMs). The network lab, MCP gateway, and orchestration stay on the host. The LLM agent process runs in the microVM. `byo.*` agents run on the host.

Implementation: [`sandbox/runner.py`](../src/agent/sandbox/runner.py) executes agents, and [`sandbox/manifest.py`](../src/agent/sandbox/manifest.py) defines the staged workspace manifest.

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
| `cli.codex` | `codex` | Host two-phase driver → `sbx exec codex exec ...` |
| `cli.claude` | `claude` | Host two-phase driver → `sbx exec claude -p ...` |
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
uv run nika failure inject link_down --set host_name=pc1 --set intf_name=eth0
uv run nika agent run -a cli.codex -m gpt-5-mini -n 20
# or Claude via DeepSeek:
# uv run nika agent run -a cli.claude -p deepseek -m deepseek-v4-flash -n 20
uv run nika session close
uv run nika eval metrics
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

Each task uses an ephemeral `results/{session_id}/.sandbox_run/` workspace (manifest and skills). `ground_truth.json` stays on the host and does not mount into the microVM. After the run, NIKA copies the standard session artifacts (`messages.jsonl`, `submission.json`, and `sandbox_manifest.json`) back and discards `.sandbox_run` plus the agent workspaces.

**Concurrent isolation:** each agent run gets its own sbx microVM (`nika-{session_id}`), workspace, and host MCP gateway on an ephemeral port. The sandbox network policy allows that session's `localhost:{port}` and blocks peer gateway ports. Parallel benchmark batches (`--batch-size N`) run one subprocess per case, so gateways do not share a process. Those processes share one host `sandboxd`. NIKA starts that daemon with `upstream_proxy` when it is down, and does not restart it while it is up. If you change the proxy, stop the daemon when no sandboxes are running (`sbx daemon stop`) and start the next NIKA run.

**Sandbox boundary:** SDK sandboxes do **not** bundle `nika/` source. The host writes MCP HTTP endpoints into `sandbox_manifest.json` (`mcp_servers`); the in-sandbox runner loads agent code, prompts/skills, and (when enabled) SDK wheels only.

### SDK agents (optional offline wheels)

`sdk.*` and SADE create a `shell` sandbox, then install Python deps after `sbx create`. Offline wheels are **off by default**; without them, packages install from PyPI inside the microVM.

Enable offline wheels to reuse host-cached dependencies across SDK/SADE sandbox runs. Host-staged wheels (`.sdk_wheels/`, cached under `.nika_cache/sbx-sdk-wheels/`) are installed with `pip --no-index`.

Package versions are pinned in [`src/agent/sandbox/sbx/requirements-sdk.txt`](../src/agent/sandbox/sbx/requirements-sdk.txt). Changing that file invalidates the wheel cache.

```yaml
# config/nika.yaml
nika:
  sandbox:
    offline_sdk_wheels: true
```

```bash
# or CLI
uv run nika agent run -a sdk.claude_sdk --sandbox-offline-sdk-wheels ...
```

### Authentication

Credentials follow Docker Sandboxes [credential isolation](https://docs.docker.com/ai/sandboxes/security/credentials/): the secret stays on the host, and the sandbox sees a sentinel or placeholder. NIKA does not copy `~/.codex/auth.json` or `~/.claude/.credentials.json` into the workspace.

#### API keys (automatic)

Put keys in the repo-root `.env`. Before `sbx create`, NIKA syncs them into sbx secrets.

```dotenv
# .env: credentials only
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# NIKA_CUSTOM_API_KEY=...
```

Select the matching provider in the run config:

```yaml
# config/nika.yaml
agent:
  provider: openai  # anthropic, deepseek, or custom
  custom:
    base_url: null  # required when provider is custom
```

| Agent | Credential path |
|-------|-----------------|
| Codex CLI / SDK | Built-in `openai` sbx secret from active provider mapping (or OAuth below) |
| Claude CLI / SDK / SADE | Native Anthropic secret, or `sbx secret set-custom` for DeepSeek/custom hosts |

#### Subscription / OAuth (interactive, once)

| Provider | User action |
|----------|-------------|
| Codex / ChatGPT | `sbx secret set -g openai --oauth` |
| Claude | `/login` inside Claude Code so the host stores the `anthropic` secret ([docs](https://docs.docker.com/ai/sandboxes/agents/claude-code/)) |

Confirm with `sbx secret ls`. Global secrets apply when a sandbox is created; recreate the sandbox after changing secrets.

## Configuration

| Flag / config | Description |
|---------------|-------------|
| `nika.sandbox.*` in `config/nika.yaml` | keep / cpus / memory / offline_sdk_wheels / upstream_proxy |
| `--sandbox-keep-container` | Keep the sandbox after agent exit (debug) |
| `--sandbox-cpus` / `--sandbox-memory` | Resource limits |
| `--sandbox-offline-sdk-wheels` | Host-cached wheels for SDK/SADE |
| `--sandbox-proxy` | Upstream proxy for the sbx daemon and host `sbx` CLI (Docker Hub auth) |

Credentials come from the repository-root `.env`; NIKA has no separate sandbox environment file.

## Troubleshoot sandbox runs

### Sandbox cannot reach an LLM API

The outbound proxy is optional and off by default. If OpenAI (or another API) fails inside the sandbox, set it in `config/nika.yaml`:

```yaml
nika:
  sandbox:
    upstream_proxy: http://127.0.0.1:7890
```

Or pass `--sandbox-proxy` on the CLI.

NIKA also sets `HTTPS_PROXY` on host `sbx` subprocesses from that URL when `HTTPS_PROXY` is unset, so `sbx create` / `sbx exec` can fetch `https://login.docker.com/.well-known/jwks.json`. If Docker Hub token refresh still times out, confirm the proxy can reach `login.docker.com`, then run `sbx login`.

## Testing

```bash
# Unit (no sbx / lab)
uv run pytest tests/agent/test_sbx.py tests/agent/test_sandbox.py -v

# Security probe (sbx + MCP gateway)
uv run pytest tests/agent/test_sandbox_security.py -v

# Cross-sandbox MCP port isolation (unit + sbx peer-gateway probe)
uv run pytest tests/agent/test_sandbox_isolation.py -v

# E2E: five sandbox agents (Codex needs OPENAI_API_KEY; Claude/SADE need DEEPSEEK_API_KEY)
uv run pytest tests/agent/test_sandbox_agents.py -v

uv run pytest tests/benchmark/test_sandbox_benchmark.py -v
```

For the full test matrix and prerequisites, see the [testing guide](testing.md).
