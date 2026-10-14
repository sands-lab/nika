# Run agents in sandboxes

Run a troubleshooting agent in an isolated sandbox while NIKA deploys the network environment and scores the diagnosis. The agent reaches the environment only through the session's MCP tools, and its model credentials stay on the NIKA host. This guide covers setup, credentials, and evaluation for CLI, SDK, and SADE agents. For host-side `byo.*` agents, use [Choose and run an agent](../agents/agent-implementations.md).

## Choose a sandbox runtime

NIKA runs sandboxes on one of two runtimes. Both run the same agents and score them the same way; they differ in where the sandbox runs and what the NIKA host needs.

| | [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`) | [E2B](https://e2b.dev/) |
|---|---|---|
| Sandboxes run | As microVMs on the NIKA host | On E2B cloud, or on a self-hosted E2B-compatible service (like AgentENV) |
| The NIKA host needs | KVM access | Network access to the service |
| Select with | `nika.sandbox.backend: sbx` (default) | `nika.sandbox.backend: e2b` |
| Subscription logins | Stored by sbx | Kept by NIKA on the host |

Choose E2B when the NIKA host has no KVM, such as a VM without nested virtualization. Both runtimes also work when the network environment runs on a [remote lab host](remote.md).

## Supported agents

| NIKA agent | Sandbox template | Additional Python dependencies |
|------------|------------------|--------------------------------|
| `cli.codex` | `codex` | None |
| `cli.claude` | `claude` | None |
| `sdk.codex_sdk` | `shell` | `uv sync --extra sdk` |
| `sdk.claude_sdk` | `shell` | `uv sync --extra sdk` |
| `community.sade` | `shell` | `uv sync --extra sade` |

The templates are Docker's `docker/sandbox-templates` images. sbx runs them directly; E2B runs templates that NIKA builds from them (see [E2B templates](#e2b-templates)).

## Set up the runtime

Both runtimes need a [NIKA installation](installation.md), a Docker engine for the network environment, and the agent's [API key or subscription login](#authentication). Then set up the runtime you chose.

### Install Docker Sandboxes

- Install `sbx` and sign in to Docker. On Linux, `./scripts/install.sh --with-sbx` also sets up KVM access:

  ```bash
  ./scripts/install.sh --with-sbx
  sbx login
  ```

- On Linux, your user needs read/write access to `/dev/kvm`, usually through the `kvm` group. `sbx diagnose` checks virtualization support. See [Manual sbx installation](installation.md#install-sbx-for-sandboxed-agents) for other install methods.
- `sbx login` authenticates Docker; agent credentials require separate setup.

### Connect to E2B

1. Install the E2B SDK. `uv sync` removes the extras you leave out, so name the ones you already use, such as `--extra sdk`:

   ```bash
   uv sync --extra e2b
   ```

2. Select the runtime in `config/nika.yaml`:

   ```yaml
   nika:
     sandbox:
       backend: e2b
   ```

3. Point NIKA at your service:

   | Service | In `.env` | In `config/nika.yaml` |
   |---------|-----------|-----------------------|
   | E2B cloud | `E2B_API_KEY` from your E2B account | Nothing else |
   | Self-hosted E2B | `E2B_API_KEY`, and `E2B_DOMAIN` with the domain of your deployment (its API runs at `api.<domain>`) | Nothing else |
   | E2B-compatible service that serves its API and sandboxes at one address (like AgentENV) | The service's API key as `E2B_API_KEY` | `nika.sandbox.url` with the service URL, such as `http://localhost:8000` |

## Authentication

Choose an API key or a supported subscription for your agent. NIKA keeps the real credential on the host; the sandbox receives only a placeholder. See [How sandboxes stay isolated](#how-sandboxes-stay-isolated).

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

Select the matching `agent.provider` in `config/nika.yaml` or pass `-p` on the CLI. With sbx, NIKA syncs the active provider's key into sbx before creating the sandbox; with E2B, NIKA's model proxy on the host adds it to the agent's requests. For a custom endpoint, also set `agent.base_url`; see [Endpoint compatibility](../agents/agent-reference.md#custom-endpoint-compatibility).

### Subscription / OAuth (interactive, once)

Your local Claude or Codex login does not authenticate a NIKA sandbox, and NIKA never copies host auth files into a sandbox. To use a Claude or ChatGPT subscription for evaluation, sign in once for the runtime you use. If `.env` also holds an API key for the provider, NIKA uses the API key.

#### Claude subscription with sbx

1. Start Claude Code in a sandbox:

   ```bash
   sbx run --name nika-claude-login claude
   ```

2. Enter `/login`, complete the browser flow, then exit Claude Code. Follow [Docker's Claude authentication instructions](https://docs.docker.com/ai/sandboxes/agents/claude-code/#authentication).

Use provider `anthropic` and a model available to your Claude subscription.

#### Codex / ChatGPT subscription with sbx

If you connect over SSH, prepare [callback port forwarding](#oauth-callback-opens-localhost-over-ssh) before opening the sign-in URL. Start the host OAuth flow and complete the browser login:

```bash
sbx secret set openai --oauth
```

Use provider `openai` and a model available to your ChatGPT account. See [Docker's Codex authentication instructions](https://docs.docker.com/ai/sandboxes/agents/codex/#authentication).

#### Verify sbx subscription credentials

```bash
sbx secret ls
```

Confirm a global `anthropic` service entry for Claude or `openai` for Codex. NIKA creates a new sandbox for each agent run, so a credential scoped to the login sandbox does not cover the evaluation.

After confirming the global Claude credential, remove the sandbox you created for login:

```bash
sbx rm nika-claude-login
```

Subsequent agent and benchmark runs reuse the sbx login. If the login expires or is revoked, sign in through sbx again.

sbx subscription logins work for the CLI agents only (`cli.claude`, `cli.codex`), because only sbx's agent templates can sign in. In other sandboxes, sbx can supply only an API key, so on sbx the SDK and SADE agents need an API key in `.env`. To run them with a subscription, use [E2B](#subscriptions-with-e2b), where NIKA adds the login on the host. Host-side `byo.*` agents use provider API credentials.

#### Subscriptions with E2B

With E2B, NIKA keeps the subscription login on the host where it runs, and its model proxy adds the login to the agent's model requests. Sign in once on that host:

- **Claude subscription:** create a long-lived token and add it to the repository-root `.env`:

  ```bash
  claude setup-token
  ```

  ```dotenv
  CLAUDE_CODE_OAUTH_TOKEN=YOUR_TOKEN
  ```

- **ChatGPT / Codex subscription:** sign in to Codex with a Codex home reserved for NIKA. NIKA refreshes this login when it expires, so keeping it separate from `~/.codex` means a refresh never signs out your own Codex:

  ```bash
  mkdir -p ~/.local/state/nika/codex
  CODEX_HOME=~/.local/state/nika/codex codex login
  ```

Use provider `anthropic` or `openai` with a model available to your subscription. The logins cover the CLI, SDK, and SADE agents. If a login stops working, sign in again with the same command.

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

NIKA deploys the environment, injects the failure, runs the agent, writes evaluation metrics, and closes the session. With E2B, the first run also builds the agent's [E2B template](#e2b-templates), which can take several minutes. Use [Inspect](session-inspect.md) to review the result. For a set of tasks, use [Benchmark configuration](../benchmarks/benchmark-configuration.md).

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

| Flag / config | Runtime | Description |
|---------------|---------|-------------|
| `nika.sandbox.*` in `config/nika.yaml` | Both | Sandbox runtime, resource limits, dependency installation, and upstream proxy |
| `--sandbox-backend sbx\|e2b` | Both | Runtime for one `nika agent run` (`nika.sandbox.backend`) |
| `nika.sandbox.url` | E2B | Address of an E2B-compatible service that serves its API and sandboxes at one address |
| `--sandbox-keep-container` | Both | Keep the sandbox after agent exit (debug). An E2B sandbox stays until `nika session close` or the end of its lifetime |
| `--sandbox-cpus` / `--sandbox-memory` | Both | Resource limits. With E2B they size the template |
| `--sandbox-offline-sdk-wheels` / `--no-sandbox-offline-sdk-wheels` | sbx | Install SDK/SADE dependencies from host-cached wheels (on by default) |
| `--sandbox-proxy` | sbx | Upstream proxy for the sbx daemon and host `sbx` CLI (Docker Hub auth) |

Use [Authentication](#authentication) to configure API keys or subscription login; NIKA has no separate sandbox environment file.

With sbx, NIKA preloads the matching `docker/sandbox-templates:*` image when ensuring environment images and at the start of agent or benchmark runs. SDK and SADE agents use the `shell` template with the dependencies below.

### SDK agents (optional offline wheels)

To reuse pinned dependencies without downloading them for each evaluation, SDK and SADE sandboxes on sbx install host-cached wheels by default. NIKA caches them under `.nika_cache/sbx-sdk-wheels/`, stages them in the sandbox workspace, and installs them with `pip --no-index`. E2B templates already contain these dependencies.

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

### E2B templates

E2B starts each sandbox from a template that the service builds and keeps. NIKA builds one template for each sandbox template an agent uses, from the same `docker/sandbox-templates` image that sbx runs, and installs the pinned SDK packages into the `shell` template. Sandboxes therefore start with everything installed, and the NIKA host uploads nothing large.

NIKA prepares the configured agent's template when it deploys an environment, when you run `uv run nika images prepare`, and before an agent run. A build can take several minutes; later runs reuse the template. NIKA builds a new template when the pinned image, the pinned SDK packages, `nika.sandbox.cpus`, or `nika.sandbox.memory` change.

## Claude Code with custom providers

When you run `cli.claude` with `agent.provider: custom` (for example, a self-hosted vLLM server), the sandbox uses the Claude Code version that ships in the `claude` sandbox template. NIKA sets `DISABLE_AUTOUPDATER=1` for every sandboxed `claude -p` call, so the version stays fixed for the whole run. The agent transcript (`messages.jsonl`) records the version as `claude_code_version` on the `init` event.

NIKA adapts each agent phase to the custom server:

| Adaptation | What NIKA does | Why |
|------------|----------------|-----|
| Context window | Reads `max_model_len` for `agent.model` from `{agent.base_url}/models`. Sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to that value and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to `min(32000, max_model_len / 8)`. | Claude Code assumes a 200k-token window for models it does not know. Without the real limit, it does not auto-compact before the server rejects a long request. |
| Message roles | Starts a host-side shim for the session and points `ANTHROPIC_BASE_URL` at it. With sbx, the sandbox reaches it at `http://host.docker.internal:<port>`, which only that sandbox may use; with E2B, through the session's tunnel. The shim merges mid-conversation `role: "system"` turns into the adjacent user turn as `<system-reminder>` text and forwards all other traffic unchanged, including streamed responses. | Claude Code sends `role: "system"` entries inside `messages` from the first request on, for example the `Today's date is …` reminder. vLLM `/v1/messages` rejects them with HTTP 400 (`Input should be 'user' or 'assistant'`). |
| Authentication | Gives Claude Code a random per-session token as its API key. The shim rejects requests without that token and sends `NIKA_CUSTOM_API_KEY` upstream when you set it. | The real endpoint key stays on the host, and other clients cannot use the shim. |

If the server does not report `max_model_len`, Claude Code keeps its defaults. NIKA sends `NIKA_CUSTOM_API_KEY` with the `/models` request when you set it. The shim reaches the endpoint from the NIKA host, so an endpoint on the host's `localhost` works. With sbx, other providers use none of these adaptations. With E2B, every provider goes through the shim, which also holds API keys and subscription logins; the context-window adaptation still applies only to `custom`. The shim lives in [`src/agent/sandbox/model_proxy.py`](../../src/agent/sandbox/model_proxy.py).

## Architecture

The network environment, MCP gateway, and orchestration run on the host. The agent runs in a sandbox and uses the gateway to inspect the environment.

```
Host (NIKA orchestration)              Sandbox (agent only)
├── Kathara / MCP gateway              ├── codex exec / claude -p  (CLI agents)
├── ground_truth.json                  └── Python SDK runner         (SDK / SADE)
├── Host phase driver (freeze + advance)   workspace = runtime/agent_workspaces/{agent_session_id}/
├── Model proxy (custom endpoints; E2B)    bundled: agent/ + skills (+ optional wheels)
└── sbx CLI or E2B API                     environment tools: MCP HTTP only
```

Each task uses an ephemeral `runtime/agent_workspaces/{agent_session_id}/` workspace (manifest and skills). sbx mounts it into the microVM; E2B copies it into the sandbox at the same path. The opaque `agent_session_id` is minted at env start and is what agents see in `NIKA_SESSION_ID`, MCP `NIKA-Session-Id`, sandbox names, and the workspace path. The human-readable host `session_id` / trial dirname (`trials/{case_key}__tNN/`) stays on the host for operators and never appears in those agent surfaces, so benchmark case keys cannot leak as shortcuts. Sessions created before this change (no `agent_session_id`) keep working: the gateway accepts the canonical id, and resolvers fall back to `session_id`.

`ground_truth.json` and host `run.json` fields such as `problem_names` stay on the host and never enter the sandbox.

The evaluated transcript, `messages.jsonl` in the host trial dir, is a host file that the sandbox cannot write:

- CLI agents: the worker runs on the host and writes `messages.jsonl` there directly.
- SDK and SADE agents: the agent writes a transcript in its workspace. The host copies new lines into its own `messages.jsonl` about once per second, so `nika inspect` and the benchmark dashboard can tail the run. The host drops any `diagnosis_frozen` event from the workspace copy and any event tagged with a phase other than the one running.

After the run, NIKA copies `sandbox_manifest.json`, with its MCP access token redacted, and discards the opaque workspace plus the agent CLI/SDK workspaces. NIKA never copies `submission.json`, `messages.jsonl`, or `nika.jsonl` out of the workspace. The host MCP `submit()` tool writes the scored `submission.json` after validation.

### Phase boundary

During diagnosis, the agent cannot reach the submission context (fault ontology and resource catalog):

1. The MCP gateway's phase-advance endpoint requires a per-session secret that the host keeps in memory. NIKA never writes that secret into the sandbox environment, workspace, or MCP config. A request without that secret gets HTTP 403.
2. CLI agents freeze the report and advance the gateway from the host process.
3. SDK and SADE agents run diagnosis in one sandbox command, which writes `diagnosis_report.json` and exits. The host then records `diagnosis_frozen` from that report, advances the gateway, writes `submission_context.json` into the workspace, and starts the submission step.

### How sandboxes stay isolated

Each agent run gets its own sandbox, workspace, and host MCP gateway on an ephemeral port. Every MCP request must carry the session's access token (`NIKA-Session-Token`) as well as its `NIKA-Session-Id`, so a client that knows or guesses a session ID still cannot use that session's gateway. Model credentials stay on the host: the sandbox receives a placeholder or a per-session proxy token.

**Docker Sandboxes:** each run uses its own sbx microVM (`nika-{agent_session_id}`). The sandbox network policy allows that session's `localhost:{port}` and blocks peer gateway ports. Parallel benchmark runs (`--batch-size N`) keep up to N trials concurrent (sliding window) and use one subprocess per active trial, so gateways do not share a process. Those processes share one host `sandboxd`. NIKA starts that daemon with `upstream_proxy` when it is down, and does not restart it while it is up. If you change the proxy, stop the daemon when no sandboxes are running (`sbx daemon stop`) and start the next NIKA run.

**E2B:** sandboxes have no internet access. NIKA connects each sandbox to the host through a tunnel over E2B's command channel, which reaches only that session's MCP gateway and model proxy. Both listen on the host's loopback interface, except that a [remote lab host](remote.md) serves the gateway. An E2B sandbox expires `agent.timeout_sec` plus 30 minutes after it starts, or after 6.5 hours when `agent.timeout_sec` is `0`. NIKA removes it when the run ends, and `nika session close` removes it after an interrupted run.

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

Or pass `--sandbox-cpus 2 --sandbox-memory 4g`. If the host has no KVM at all, use [E2B](#connect-to-e2b) instead.

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
4. When the remote command reports success, stop the tunnel with Ctrl+C and [verify the sbx credential](#verify-sbx-subscription-credentials).

If the OAuth command timed out, restart it and check the new callback port before signing in again. See [OpenAI's SSH callback guidance](https://learn.chatgpt.com/docs/auth#fallback-forward-the-localhost-callback-over-ssh). The same forwarding applies to `codex login` for an [E2B Codex subscription](#subscriptions-with-e2b); take its callback port from the sign-in URL the same way.

### Sandbox cannot reach an LLM API

With sbx, the outbound proxy is optional and off by default. If OpenAI (or another API) fails inside the sandbox, set it in `config/nika.yaml`:

```yaml
nika:
  sandbox:
    upstream_proxy: http://127.0.0.1:7890
```

Or pass `--sandbox-proxy` on the CLI.

NIKA also sets `HTTPS_PROXY` on host `sbx` subprocesses from that URL when `HTTPS_PROXY` is unset, so `sbx create` / `sbx exec` can fetch `https://login.docker.com/.well-known/jwks.json`. If Docker Hub token refresh still times out, confirm the proxy can reach `login.docker.com`, then run `sbx login`.

With E2B, model requests leave from the NIKA host through its model proxy, so the host itself must reach the provider's API.

### E2B runs stop before the sandbox starts

- `The e2b sandbox backend needs the E2B SDK` means the `e2b` extra is missing; run `uv sync --extra e2b` with your other extras.
- `Set E2B_API_KEY in .../.env` means NIKA found no service key; see [Connect to E2B](#connect-to-e2b).
- `Could not build e2b template` means the service rejected the template build. The service must implement E2B's template build API. The next run removes the failed template and builds it again.
- `No API key or subscription login for provider` means the provider has neither an API key in `.env` nor an [E2B subscription login](#subscriptions-with-e2b).

The [agent test matrix](../development/testing.md#agent-tests-testsagent) lists sandbox unit, security, isolation, and end-to-end commands with their prerequisites.
