# Run agents in Docker Sandboxes

This guide is for operators who run NIKA CLI, SDK, or SADE agents in [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx` microVMs). The network lab, MCP gateway, and orchestration stay on the host. The LLM agent process runs in the microVM. `byo.*` agents run on the host.

## How it works

NIKA uses **native sbx agent templates** (`codex`, `claude`, `shell`) from Docker Sandboxes.

| Concern | Behavior |
|---------|----------|
| Isolation | per-session microVM + workspace; MCP gateway on ephemeral port; sbx policy blocks peer ports |
| Agent binaries | Official `codex` / `claude` / `shell` sandbox templates; `cli.claude` with a custom provider sends LLM requests through a host shim ([details](#claude-code-with-custom-providers)) |
| Credentials | Host `sbx secret` store; microVM sees placeholders (`proxy-managed` / custom placeholders) only |
| Network policy | `sbx policy allow network` (MCP gateway + LLM hosts such as `api.deepseek.com`) |
| Orchestration | Host two-phase driver; agent phases via `sbx exec`; the host freezes the diagnosis report and advances the MCP gateway between phases |
| Lab / tools | Host MCP HTTP gateway only (no lab binaries inside the VM) |

## Supported agents

| NIKA agent | Native sbx agent | Execution model |
|------------|------------------|-----------------|
| `cli.codex` | `codex` | Host two-phase driver → `sbx exec codex exec ...` |
| `cli.claude` | `claude` | Host two-phase driver → `sbx exec claude -p ...` |
| `sdk.codex_sdk` | `shell` (+ optional offline SDK wheels) | In-sandbox Python runner, one `sbx exec` per phase |
| `sdk.claude_sdk` | `shell` (+ optional offline SDK wheels) | In-sandbox Python runner, one `sbx exec` per phase |
| `community.sade` | `shell` (+ optional offline SDK wheels) | In-sandbox Python runner, one `sbx exec` per phase |

### Claude Code with custom providers

When you run `cli.claude` with `agent.provider: custom` (for example, a self-hosted vLLM server), the sandbox uses the Claude Code version that ships in the `claude` sandbox template. NIKA sets `DISABLE_AUTOUPDATER=1` for every sandboxed `claude -p` call, so the version stays fixed for the whole run. The agent transcript (`messages.jsonl`) records the version as `claude_code_version` on the `init` event.

NIKA adapts each agent phase to the custom server:

| Adaptation | What NIKA does | Why |
|------------|----------------|-----|
| Context window | Reads `max_model_len` for `agent.model` from `{agent.custom.base_url}/models`. Sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to that value and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to `min(32000, max_model_len / 8)`. | Claude Code assumes a 200k-token window for models it does not know. Without the real limit, it does not auto-compact before the server rejects a long request. |
| Message roles | Starts a host-side shim for the phase, points `ANTHROPIC_BASE_URL` at `http://host.docker.internal:<port>`, and allows that port for the phase's sandbox only. The shim merges mid-conversation `role: "system"` turns into the adjacent user turn as `<system-reminder>` text and forwards all other traffic unchanged, including streamed responses. | Claude Code sends `role: "system"` entries inside `messages`, for example after auto-compaction. vLLM `/v1/messages` rejects them with HTTP 400 (`Input should be 'user' or 'assistant'`). |
| Authentication | Gives Claude Code a random per-phase token as its API key. The shim rejects requests without that token and sends `NIKA_CUSTOM_API_KEY` upstream when you set it. | The real endpoint key stays on the host, and other clients on the host bridge cannot use the shim. |

If the server does not report `max_model_len`, Claude Code keeps its defaults. NIKA sends `NIKA_CUSTOM_API_KEY` with the `/models` request when you set it. Other providers use none of these adaptations. The shim lives in [`src/agent/cli/claude/vllm_shim.py`](../../src/agent/cli/claude/vllm_shim.py).

## Prerequisites

- `sbx` CLI installed and logged in. `./scripts/install.sh --with-sbx` installs it and sets up KVM access (see [Installation](installation.md#install-sbx-for-sandboxed-agents)):

  ```shell
  ./scripts/install.sh --with-sbx
  sbx login
  ```

  To install manually: `curl -fsSL https://get.docker.com | sudo SBX=1 sh`, or `sudo apt install docker-sbx` on a host that already has Docker's apt repo. See the [Docker Sandboxes install guide](https://docs.docker.com/ai/sandboxes/install/).
- KVM available on Linux (`/dev/kvm`); your user must be in the `kvm` group (or otherwise have read/write on the device). `--with-sbx` adds you to that group. `sbx diagnose` reports this under Virtualization.
- Docker for Kathara / Containerlab labs
- Credentials for the agent you run (see [Authentication](#authentication))

When the run config (or CLI) selects a sandbox-supported agent (`cli.codex`,
`cli.claude`, `sdk.*`, `community.sade`), NIKA preloads the matching
`docker/sandbox-templates:*` image during lab image ensure and at the start of
`nika agent run` / benchmark jobs, not on each case's `sbx create`.

## Quick start

Task mode deploys the lab, injects the fault, runs the agent, closes the session, and writes metrics in one command:

```bash
uv run nika agent run -a cli.codex -m gpt-5-mini -n 20 --problem dc_clos_s_link_down
# or Claude Code through DeepSeek:
# uv run nika agent run -a cli.claude -p deepseek -m deepseek-v4-flash -n 20 --problem dc_clos_s_link_down
```

Session mode gives you control of each step. `nika env run` prints the `session_id` that the last command needs:

```bash
uv run nika env run dc_clos -s s
uv run nika failure inject link_down --set host_name=pc_0_0 --set intf_name=eth0
uv run nika agent run -a cli.codex -m gpt-5-mini -n 20
uv run nika session close -y
uv run nika eval metrics --session_id <session_id>
```

`-n` sets the LLM-turn budget per phase for every agent. See [Step limit](../agents/agent-implementations.md#step-limit).

## Architecture

```
Host (NIKA orchestration)          sbx microVM (agent-only)
├── Kathara / MCP gateway          ├── codex exec / claude -p  (CLI agents)
├── ground_truth.json              └── Python SDK runner         (SDK / SADE)
├── Host phase driver (freeze + advance) workspace = runtime/agent_workspaces/{agent_session_id}/
└── sbx create / exec / policy         bundled: agent/ + skills (+ optional wheels)
                                       lab interaction: MCP HTTP only
```

Each task uses an ephemeral `runtime/agent_workspaces/{agent_session_id}/` workspace (manifest and skills). The opaque `agent_session_id` is minted at env start and is what agents see in `NIKA_SESSION_ID`, MCP `NIKA-Session-Id`, sandbox hostname, and the bind-mount path. The human-readable host `session_id` / trial dirname (`trials/{case_key}__tNN/`) stays on the host for operators and never appears in those agent surfaces, so benchmark case keys cannot leak as shortcuts. Sessions created before this change (no `agent_session_id`) keep working: the gateway accepts the canonical id, and resolvers fall back to `session_id`.

`ground_truth.json` and host `run.json` fields such as `problem_names` stay on the host and do not mount into the microVM.

The evaluated transcript, `messages.jsonl` in the host trial dir, is a host file that the microVM cannot write:

- CLI agents: the worker runs on the host and writes `messages.jsonl` there directly.
- SDK and SADE agents: the agent writes a transcript in its workspace. The host copies new lines into its own `messages.jsonl` about once per second, so `nika inspect` and the benchmark dashboard can tail the run. The host drops any `diagnosis_frozen` event from the workspace copy and any event tagged with a phase other than the one running.

After the run, NIKA copies `sandbox_manifest.json` and discards the opaque workspace plus the agent CLI/SDK workspaces. NIKA never copies `submission.json`, `messages.jsonl`, or `nika.jsonl` out of the workspace. The host MCP `submit()` tool writes the scored `submission.json` after validation.

### Phase boundary

The agent cannot reach the submission context (fault ontology and resource catalog) during diagnosis:

1. The MCP gateway's phase-advance endpoint requires a per-session secret that the host keeps in memory. NIKA never writes that secret into the sandbox environment, workspace, or MCP config. A request that carries only the `NIKA-Session-Id` header gets HTTP 403.
2. CLI agents freeze the report and advance the gateway from the host process.
3. SDK and SADE agents run diagnosis in one `sbx exec` step, which writes `diagnosis_report.json` and exits. The host then records `diagnosis_frozen` from that report, advances the gateway, writes `submission_context.json` into the workspace, and starts the submission step.

**Concurrent isolation:** each agent run gets its own sbx microVM (`nika-{agent_session_id}`), workspace, and host MCP gateway on an ephemeral port. The sandbox network policy allows that session's `localhost:{port}` and blocks peer gateway ports. Parallel benchmark runs (`--batch-size N`) keep up to N trials concurrent (sliding window) and use one subprocess per active trial, so gateways do not share a process. Those processes share one host `sandboxd`. NIKA starts that daemon with `upstream_proxy` when it is down, and does not restart it while it is up. If you change the proxy, stop the daemon when no sandboxes are running (`sbx daemon stop`) and start the next NIKA run.

**Sandbox boundary:** SDK sandboxes do **not** bundle `nika/` source. The host writes MCP HTTP endpoints into `sandbox_manifest.json` (`mcp_servers`); the in-sandbox runner loads agent code, prompts/skills, and (when enabled) SDK wheels only.

### SDK agents (optional offline wheels)

`sdk.*` and SADE create a `shell` sandbox, then install Python deps after `sbx create`. Offline wheels are **on by default**: NIKA stages host-cached wheels (`.sdk_wheels/`, cached under `.nika_cache/sbx-sdk-wheels/`) and installs them with `pip --no-index`, so SDK/SADE sandbox runs reuse the same dependencies.

Disable offline wheels to install packages from PyPI inside the microVM instead.

Package versions are pinned in [`src/agent/sandbox/sbx/requirements-sdk.txt`](../../src/agent/sandbox/sbx/requirements-sdk.txt). Changing that file invalidates the wheel cache.

```yaml
# config/nika.yaml
nika:
  sandbox:
    offline_sdk_wheels: false
```

```bash
# or CLI
uv run nika agent run -a sdk.claude_sdk --no-sandbox-offline-sdk-wheels ...
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
| `--sandbox-offline-sdk-wheels` / `--no-sandbox-offline-sdk-wheels` | Host-cached wheels for SDK/SADE (on by default) |
| `--sandbox-proxy` | Upstream proxy for the sbx daemon and host `sbx` CLI (Docker Hub auth) |

Credentials come from the repository-root `.env`; NIKA has no separate sandbox environment file.

## Troubleshoot sandbox runs

### `sbx create` fails with a KVM error

`KVM error: Permission denied` in `~/.local/state/sandboxes/sandboxes/sandboxd/daemon.log` means your user cannot open `/dev/kvm`. Run `./scripts/install.sh --with-sbx`, open a new login shell, then run `sbx daemon stop` so the next NIKA run starts the daemon with the new group.

`VM did not connect within 15s` means the sandbox microVM took longer than the fixed sbx boot timeout. It happens when several microVMs boot at once on a loaded host, or when the host is itself a VM (nested virtualization) and the sandbox gets every host vCPU, which is the sbx default when `cpus` is `null`.

NIKA serializes `sbx create` across all NIKA processes on the host, so parallel benchmark trials boot one microVM at a time. Agent runs still overlap. NIKA also sizes each sandbox at 2 vCPUs and 4 GiB by default. If boots still time out, check that `config/nika.yaml` does not set `cpus: null` or a large size:

```yaml
nika:
  sandbox:
    cpus: 2
    memory: 4g
```

Or pass `--sandbox-cpus 2 --sandbox-memory 4g`.

### Sandbox cannot reach an LLM API

The outbound proxy is optional and off by default. If OpenAI (or another API) fails inside the sandbox, set it in `config/nika.yaml`:

```yaml
nika:
  sandbox:
    upstream_proxy: http://127.0.0.1:7890
```

Or pass `--sandbox-proxy` on the CLI.

NIKA also sets `HTTPS_PROXY` on host `sbx` subprocesses from that URL when `HTTPS_PROXY` is unset, so `sbx create` / `sbx exec` can fetch `https://login.docker.com/.well-known/jwks.json`. If Docker Hub token refresh still times out, confirm the proxy can reach `login.docker.com`, then run `sbx login`.

The [agent test matrix](../development/testing.md#agent-tests-testsagent) lists sandbox unit, security, isolation, and end-to-end commands with their prerequisites.
