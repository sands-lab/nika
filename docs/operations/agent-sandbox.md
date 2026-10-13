# Run agents in Docker Sandboxes

Run a troubleshooting agent in an isolated [Docker Sandbox](https://docs.docker.com/ai/sandboxes/) while NIKA deploys the network environment and scores the diagnosis. This guide covers setup, credentials, and evaluation for CLI, SDK, and SADE agents. For host-side `byo.*` agents, use [Choose and run an agent](../agents/agent-implementations.md).

## Supported agents

| NIKA agent | Native sbx template | Additional Python dependencies |
|------------|---------------------|--------------------------------|
| `cli.codex` | `codex` | None |
| `cli.claude` | `claude` | None |
| `sdk.codex_sdk` | `shell` | `uv sync --extra sdk` |
| `sdk.claude_sdk` | `shell` | `uv sync --extra sdk` |
| `community.sade` | `shell` | `uv sync --extra sade` |

## Prerequisites

- Complete [NIKA installation](installation.md).
- Install `sbx` and sign in to Docker. On Linux, `./scripts/install.sh --with-sbx` also sets up KVM access:

  ```bash
  ./scripts/install.sh --with-sbx
  sbx login
  ```

- On Linux, your user needs read/write access to `/dev/kvm`, usually through the `kvm` group. `sbx diagnose` checks virtualization support. See [Manual sbx installation](installation.md#install-sbx-for-sandboxed-agents) for other install methods.
- Run a Docker engine for the network environment.
- Configure the agent's [API key or subscription login](#authentication) before starting an evaluation. `sbx login` authenticates Docker; agent credentials require separate setup.

## Authentication

Choose an API key or a supported subscription for your agent. NIKA keeps credentials in the host's sbx secret store; the sandbox receives placeholders. See [Docker credential isolation](https://docs.docker.com/ai/sandboxes/configuration/credentials/).

### API keys (automatic)

Set the key for your chosen provider in the repository-root `.env`:

| Provider | Credential |
|----------|------------|
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `custom` | `NIKA_CUSTOM_API_KEY`, if the endpoint requires authentication |

For example, to use an OpenAI API key:

```dotenv
OPENAI_API_KEY=YOUR_API_KEY
```

Select the matching `agent.provider` in `config/nika.yaml` or pass `-p` on the CLI. NIKA syncs the active provider's key into sbx before creating the sandbox. For a custom endpoint, also set `agent.base_url`; see [Endpoint compatibility](../agents/agent-reference.md#custom-endpoint-compatibility).

### Subscription / OAuth (interactive, once)

Your local Claude or Codex login does not authenticate a NIKA sandbox. To use a Claude or ChatGPT subscription for evaluation, first sign in through sbx. NIKA does not import local OAuth logins or copy host auth files into the sandbox.

#### Claude subscription

1. Start Claude Code in a sandbox:

   ```bash
   sbx run --name nika-claude-login claude
   ```

2. Enter `/login`, complete the browser flow, then exit Claude Code. Follow [Docker's Claude authentication instructions](https://docs.docker.com/ai/sandboxes/agents/claude-code/#authentication).

Use provider `anthropic` and a model available to your Claude subscription.

#### Codex / ChatGPT subscription

If you connect over SSH, prepare [callback port forwarding](#oauth-callback-opens-localhost-over-ssh) before opening the sign-in URL. Start the host OAuth flow and complete the browser login:

```bash
sbx secret set openai --oauth
```

Use provider `openai` and a model available to your ChatGPT account. See [Docker's Codex authentication instructions](https://docs.docker.com/ai/sandboxes/agents/codex/#authentication).

#### Verify subscription credentials

```bash
sbx secret ls
```

Confirm a global `anthropic` service entry for Claude or `openai` for Codex. NIKA creates a new sandbox for each agent run, so a credential scoped to the login sandbox does not cover the evaluation.

After confirming the global Claude credential, remove the sandbox you created for login:

```bash
sbx rm nika-claude-login
```

Subsequent agent and benchmark runs reuse the sbx login. If the login expires or is revoked, sign in through sbx again. This setup also applies to the corresponding SDK/SADE agents; host-side `byo.*` agents use provider API credentials.

## Quick start

Set `agent.type`, `agent.provider`, and `agent.model` in `config/nika.yaml`. For a Claude subscription, replace `YOUR_CLAUDE_MODEL` with a model available to your account:

```yaml
agent:
  type: cli.claude
  provider: anthropic
  model: YOUR_CLAUDE_MODEL
```

For Codex, use `type: cli.codex`, `provider: openai`, and an accessible model. Check the active settings with `uv run nika config show`.

Run a task with the configured agent:

```bash
uv run nika agent run --problem dc_clos_s_link_down
```

NIKA deploys the environment, injects the failure, runs the agent, writes evaluation metrics, and closes the session. Use [Inspect](session-inspect.md) to review the result. For a set of tasks, use [Benchmark configuration](../benchmarks/benchmark-configuration.md).

To control each stage, run the same agent on an existing session. `nika env run` prints the `session_id` used to inspect metrics:

```bash
uv run nika env run dc_clos -s s
uv run nika failure inject link_down --set host_name=client_0 --set intf_name=eth0
uv run nika agent run
uv run nika session close -y
uv run nika eval metrics --session_id <session_id>
```

Use `-n` to change the LLM-turn budget per phase; see [Step limit](../agents/agent-reference.md#step-limit).

## Configuration

| Flag / config | Description |
|---------------|-------------|
| `nika.sandbox.*` in `config/nika.yaml` | Sandbox lifetime, resource limits, dependency installation, and upstream proxy |
| `--sandbox-keep-container` | Keep the sandbox after agent exit (debug) |
| `--sandbox-cpus` / `--sandbox-memory` | Resource limits |
| `--sandbox-offline-sdk-wheels` / `--no-sandbox-offline-sdk-wheels` | Install SDK/SADE dependencies from host-cached wheels (on by default) |
| `--sandbox-proxy` | Upstream proxy for the sbx daemon and host `sbx` CLI (Docker Hub auth) |

Use [Authentication](#authentication) to configure API keys or subscription login; NIKA has no separate sandbox environment file.

NIKA preloads the matching `docker/sandbox-templates:*` image when ensuring environment images and at the start of agent or benchmark runs. SDK and SADE agents use the `shell` template with the dependencies below.

### SDK agents (optional offline wheels)

To reuse pinned dependencies without downloading them for each evaluation, SDK and SADE sandboxes install host-cached wheels by default. NIKA caches them under `.nika_cache/sbx-sdk-wheels/`, stages them in the sandbox workspace, and installs them with `pip --no-index`.

If the host cannot cache the wheels, disable offline installation to download packages from PyPI inside the microVM:

```yaml
# config/nika.yaml
nika:
  sandbox:
    offline_sdk_wheels: false
```

```bash
# or CLI
uv run nika agent run -a sdk.claude_sdk --no-sandbox-offline-sdk-wheels \
  --problem dc_clos_s_link_down
```

Package versions are pinned in [`src/agent/sandbox/requirements-sdk.txt`](../../src/agent/sandbox/requirements-sdk.txt). Changing that file invalidates the wheel cache.

## Claude Code with custom providers

When you run `cli.claude` with `agent.provider: custom` (for example, a self-hosted vLLM server), the sandbox uses the Claude Code version that ships in the `claude` sandbox template. NIKA sets `DISABLE_AUTOUPDATER=1` for every sandboxed `claude -p` call, so the version stays fixed for the whole run. The agent transcript (`messages.jsonl`) records the version as `claude_code_version` on the `init` event.

NIKA adapts each agent phase to the custom server:

| Adaptation | What NIKA does | Why |
|------------|----------------|-----|
| Context window | Reads `max_model_len` for `agent.model` from `{agent.base_url}/models`. Sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to that value and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to `min(32000, max_model_len / 8)`. | Claude Code assumes a 200k-token window for models it does not know. Without the real limit, it does not auto-compact before the server rejects a long request. |
| Message roles | Starts a host-side shim for the phase, points `ANTHROPIC_BASE_URL` at `http://host.docker.internal:<port>`, and allows that port for the phase's sandbox only. The shim merges mid-conversation `role: "system"` turns into the adjacent user turn as `<system-reminder>` text and forwards all other traffic unchanged, including streamed responses. | Claude Code sends `role: "system"` entries inside `messages` from the first request on, for example the `Today's date is …` reminder. vLLM `/v1/messages` rejects them with HTTP 400 (`Input should be 'user' or 'assistant'`). |
| Authentication | Gives Claude Code a random per-phase token as its API key. The shim rejects requests without that token and sends `NIKA_CUSTOM_API_KEY` upstream when you set it. | The real endpoint key stays on the host, and other clients on the host bridge cannot use the shim. |

If the server does not report `max_model_len`, Claude Code keeps its defaults. NIKA sends `NIKA_CUSTOM_API_KEY` with the `/models` request when you set it. Other providers use none of these adaptations. The shim lives in [`src/agent/sandbox/model_proxy.py`](../../src/agent/sandbox/model_proxy.py).

## Architecture

The network environment, MCP gateway, and orchestration run on the host. The agent runs in an sbx microVM and uses the gateway to inspect the environment. NIKA allows the session gateway and the selected provider hosts through sbx network policy.

```
Host (NIKA orchestration)          sbx microVM (agent-only)
├── Kathara / MCP gateway          ├── codex exec / claude -p  (CLI agents)
├── ground_truth.json              └── Python SDK runner         (SDK / SADE)
├── Host phase driver (freeze + advance) workspace = runtime/agent_workspaces/{agent_session_id}/
└── sbx create / exec / policy         bundled: agent/ + skills (+ optional wheels)
                                       environment tools: MCP HTTP only
```

Each task uses an ephemeral `runtime/agent_workspaces/{agent_session_id}/` workspace (manifest and skills). The opaque `agent_session_id` is minted at env start and is what agents see in `NIKA_SESSION_ID`, MCP `NIKA-Session-Id`, sandbox hostname, and the bind-mount path. The human-readable host `session_id` / trial dirname (`trials/{case_key}__tNN/`) stays on the host for operators and never appears in those agent surfaces, so benchmark case keys cannot leak as shortcuts. Sessions created before this change (no `agent_session_id`) keep working: the gateway accepts the canonical id, and resolvers fall back to `session_id`.

`ground_truth.json` and host `run.json` fields such as `problem_names` stay on the host and do not mount into the microVM.

The evaluated transcript, `messages.jsonl` in the host trial dir, is a host file that the microVM cannot write:

- CLI agents: the worker runs on the host and writes `messages.jsonl` there directly.
- SDK and SADE agents: the agent writes a transcript in its workspace. The host copies new lines into its own `messages.jsonl` about once per second, so `nika inspect` and the benchmark dashboard can tail the run. The host drops any `diagnosis_frozen` event from the workspace copy and any event tagged with a phase other than the one running.

After the run, NIKA copies `sandbox_manifest.json` and discards the opaque workspace plus the agent CLI/SDK workspaces. NIKA never copies `submission.json`, `messages.jsonl`, or `nika.jsonl` out of the workspace. The host MCP `submit()` tool writes the scored `submission.json` after validation.

### Phase boundary

During diagnosis, the agent cannot reach the submission context (fault ontology and resource catalog):

1. The MCP gateway's phase-advance endpoint requires a per-session secret that the host keeps in memory. NIKA never writes that secret into the sandbox environment, workspace, or MCP config. A request that carries only the `NIKA-Session-Id` header gets HTTP 403.
2. CLI agents freeze the report and advance the gateway from the host process.
3. SDK and SADE agents run diagnosis in one `sbx exec` step, which writes `diagnosis_report.json` and exits. The host then records `diagnosis_frozen` from that report, advances the gateway, writes `submission_context.json` into the workspace, and starts the submission step.

**Concurrent isolation:** each agent run gets its own sbx microVM (`nika-{agent_session_id}`), workspace, and host MCP gateway on an ephemeral port. The sandbox network policy allows that session's `localhost:{port}` and blocks peer gateway ports. Parallel benchmark runs (`--batch-size N`) keep up to N trials concurrent (sliding window) and use one subprocess per active trial, so gateways do not share a process. Those processes share one host `sandboxd`. NIKA starts that daemon with `upstream_proxy` when it is down, and does not restart it while it is up. If you change the proxy, stop the daemon when no sandboxes are running (`sbx daemon stop`) and start the next NIKA run.

**Sandbox boundary:** SDK sandboxes do **not** bundle `nika/` source. The host writes MCP HTTP endpoints into `sandbox_manifest.json` (`mcp_servers`); the in-sandbox runner loads agent code, prompts/skills, and (when enabled) SDK wheels only.

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

### OAuth callback opens localhost over SSH

When you run `sbx secret set openai --oauth` over SSH, the callback server runs on the remote host. Your browser opens `localhost` on your own computer, so sign-in cannot finish until you forward the callback port.

1. Keep the remote OAuth command running. Find the callback port in the authorization URL's `redirect_uri` or the browser's failed callback address. Use that port; sbx can differ from the Codex CLI's default port.
2. In another terminal on your own computer, create a tunnel to the same SSH server. For example, if the callback uses port `5411`:

   ```bash
   ssh -N -o ExitOnForwardFailure=yes \
     -L 127.0.0.1:5411:127.0.0.1:5411 ubuntu@YOUR_SSH_HOST
   ```

   Replace both port numbers with your callback port and use your SSH username and host.
3. Leave the tunnel open while completing the browser login. If the browser already reached the failed callback page, refresh that page after opening the tunnel.
4. When the remote command reports success, stop the tunnel with Ctrl+C and [verify the sbx credential](#verify-subscription-credentials).

If the OAuth command timed out, restart it and check the new callback port before signing in again. See [OpenAI's SSH callback guidance](https://learn.chatgpt.com/docs/auth#fallback-forward-the-localhost-callback-over-ssh).

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
