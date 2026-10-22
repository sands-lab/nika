# Run agents in sandboxes

Every agent runs in its own isolated sandbox while NIKA deploys the network environment and scores the diagnosis. The agent reaches the environment only through the session's MCP tools, its model credentials stay on the NIKA host, and nothing in the sandbox names NIKA, so an agent cannot tell which benchmark runs it. This guide covers setup, credentials, and evaluation. To pick an agent, see [Choose and run an agent](../agents/agent-implementations.md).

## Choose a sandbox runtime

NIKA runs every agent in a sandbox, so you need one sandbox runtime. Each one runs the same agents and scores them the same way; they differ in what the NIKA host needs.

| | [AgentENV](https://github.com/kvcache-ai/AgentENV) in Docker (recommended) | [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`) | [E2B](https://e2b.dev/) cloud |
|---|---|---|---|
| Sandboxes run | As microVMs in an open-source, E2B-compatible service on the NIKA host | As microVMs on the NIKA host | On E2B's servers |
| The NIKA host needs | KVM, kernel 6.8+, and a privileged container | KVM and a Docker account | Network access and an E2B account |
| Set up with | `./scripts/install.sh --with-agentenv` | `./scripts/install.sh --with-sbx`, then `sbx login` | An `E2B_API_KEY` in `.env` |
| Selected by | `nika.sandbox.backend: e2b` and `nika.sandbox.url` | `nika.sandbox.backend: sbx` (default) | `nika.sandbox.backend: e2b` |

AgentENV needs no account and keeps everything on your host. Docker Sandboxes also stays on the host but requires signing in to Docker. Without KVM, such as on a VM without nested virtualization, use E2B cloud. Every runtime also works when the network environment runs on a [remote lab host](remote.md).

## Supported agents

| NIKA agent | Sandbox template | Python packages in the sandbox |
|------------|------------------|--------------------------------|
| `byo.langgraph` | `shell` | `langgraph` |
| `byo.mcp_agent` | `shell` | `mcp_agent` |
| `byo.autogen` | `shell` | `autogen` |
| `cli.codex` | `codex` | `runtime` |
| `cli.claude` | `claude` | `runtime` |
| `sdk.codex_sdk` | `shell` | `codex_sdk` |
| `sdk.claude_sdk` | `shell` | `claude_sdk` |
| `community.sade` | `shell` | `claude_sdk` |
| [Custom agents](../agents/custom-agents.md) (`custom.<name>`) | `shell` | `langgraph`, plus the class's `dependencies` |
| [Added agents](../agents/agent-reference.md#custom-agent-registration) | `shell`, or the manifest's `sandbox.base` | `langgraph`, plus the agent's own dependencies |

The templates are Docker's `docker/sandbox-templates` images. sbx runs them directly; E2B runs templates that NIKA builds from them (see [E2B templates](#e2b-templates)). Each package set is pinned in [`src/nika/agents/sandbox/requirements/`](../../src/nika/agents/sandbox/requirements/), so agents need nothing installed in NIKA's environment. Only the `mock` test agent runs in the NIKA process, because it reads the ground truth.

## Set up the runtime

Every runtime needs a [NIKA installation](installation.md), a Docker engine for the network environment, and the agent's [API key or subscription login](#authentication). Then set up the runtime you chose.

### Run AgentENV in Docker

Run the installer with `--with-agentenv`:

```bash
./scripts/install.sh --with-agentenv
```

It loads the `ublk_drv` kernel module that AgentENV needs (and loads it again at boot), starts AgentENV as the `nika-agentenv` container on `127.0.0.1:8800` (set `AGENTENV_PORT` to change it), writes its API key to `.env` as `E2B_API_KEY`, and sets `nika.sandbox.backend: e2b` and `nika.sandbox.url: http://127.0.0.1:8800` in `config/nika.yaml`. The container restarts with Docker. `./scripts/uninstall.sh` removes it.

The container runs privileged with the host's `/dev`, because AgentENV starts each sandbox as a Firecracker microVM. Its API has no TLS, so the installer publishes it on the loopback address only. For many concurrent sandboxes, also apply AgentENV's host tuning in [`docker-setup.sh`](https://github.com/kvcache-ai/AgentENV/blob/main/scripts/docker-setup.sh).

### Install Docker Sandboxes

- Install `sbx` and sign in to Docker. On Linux, `./scripts/install.sh --with-sbx` also sets up KVM access:

  ```bash
  ./scripts/install.sh --with-sbx
  sbx login
  ```

- On Linux, your user needs read/write access to `/dev/kvm`, usually through the `kvm` group. `sbx diagnose` checks virtualization support. See [Manual sbx installation](installation.md#install-a-sandbox-runtime) for other install methods.
- `sbx login` authenticates Docker; agent credentials require separate setup.

### Connect to E2B cloud or another E2B-compatible service

1. Select the runtime in `config/nika.yaml`:

   ```yaml
   nika:
     sandbox:
       backend: e2b
   ```

2. Point NIKA at your service:

   | Service | In `.env` | In `config/nika.yaml` |
   |---------|-----------|-----------------------|
   | E2B cloud | `E2B_API_KEY` from your E2B account | Nothing else |
   | Self-hosted E2B | `E2B_API_KEY`, and `E2B_DOMAIN` with the domain of your deployment (its API runs at `api.<domain>`) | Nothing else |
   | E2B-compatible service that serves its API and sandboxes at one address (like AgentENV on another host) | The service's API key as `E2B_API_KEY` | `nika.sandbox.url` with the service URL |

## Authentication

Every agent works with an API key. Codex and Claude agents (`cli.codex`, `sdk.codex_sdk`, `cli.claude`, `sdk.claude_sdk`, `community.sade`) can use a Claude or ChatGPT subscription instead. NIKA keeps the real credential on the host: every model request of a sandbox goes through NIKA's model proxy on the host, which adds the credential, so the sandbox only receives a per-session proxy token. See [How sandboxes stay isolated](#how-sandboxes-stay-isolated).

### API keys

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

Select the matching `agent.provider` in `config/nika.yaml` or pass `-p` on the CLI. For a custom endpoint, also set `agent.base_url`; see [Endpoint compatibility](../agents/agent-reference.md#custom-endpoint-compatibility).

### Subscriptions

Subscriptions work with Codex and Claude agents only; BYO agents and agents you add need an API key. Your local Claude or Codex login does not authenticate a NIKA sandbox, and NIKA never copies host auth files into a sandbox. To evaluate with a Claude or ChatGPT subscription, sign in once on the NIKA host; NIKA's model proxy adds the login to the agent's model requests. If `.env` also holds an API key for the provider, NIKA uses the API key.

- **Claude subscription:** create a long-lived token and add it to the repository-root `.env`:

  ```bash
  claude setup-token
  ```

  ```dotenv
  CLAUDE_CODE_OAUTH_TOKEN=YOUR_TOKEN
  ```

- **ChatGPT / Codex subscription:** sign in to Codex with a Codex home reserved for NIKA. NIKA refreshes this login when it expires, so keeping it separate from `~/.codex` means a refresh never signs out your own Codex. Over SSH, prepare [callback port forwarding](#oauth-callback-opens-localhost-over-ssh) first:

  ```bash
  mkdir -p ~/.local/state/nika/codex
  CODEX_HOME=~/.local/state/nika/codex codex login
  ```

Use provider `anthropic` or `openai` with a model available to your subscription. The logins work on every runtime. If a login stops working, sign in again with the same command.

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
| `--sandbox-offline-sdk-wheels` / `--no-sandbox-offline-sdk-wheels` | sbx | Install the agent's Python packages from host-cached wheels (on by default) |
| `--sandbox-proxy` | sbx | Upstream proxy for the sbx daemon and host `sbx` CLI (Docker Hub auth) |

Use [Authentication](#authentication) to configure API keys or subscription login; NIKA has no separate sandbox environment file.

With sbx, NIKA preloads the matching `docker/sandbox-templates:*` image when ensuring environment images and at the start of agent or benchmark runs.

### Agent packages (optional offline wheels)

To reuse pinned packages without downloading them for each evaluation, sbx sandboxes install host-cached wheels by default. NIKA caches each package set under `.nika_cache/agent-wheels/<set>/`, stages it in the sandbox workspace, and installs it with `pip --no-index`. The host downloads the wheels with `python3 -m pip`, or with `uvx` when the system Python has no pip. A custom or added agent's own dependencies always come from PyPI, and so does everything for an agent whose `requires_python` excludes the sandbox's Python. E2B templates already contain the packages.

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

The committed pins are the source of truth for sandboxes. Top-level packages match the exact pins in `pyproject.toml`; after changing a set's `.in` file or those pins, regenerate the sets with `scripts/compile_agent_requirements.sh`, which resolves them against the local `uv.lock`. A changed set invalidates its wheel cache.

### E2B templates

E2B starts each sandbox from a template that the service builds and keeps. NIKA builds one template for each sandbox template an agent uses, from the same `docker/sandbox-templates` image that sbx runs, and installs the agent's pinned packages into it. Sandboxes therefore start with everything installed, and the NIKA host uploads nothing large.

NIKA prepares the configured agent's template when it deploys an environment, when you run `uv run nika images prepare`, and before an agent run. A build can take several minutes; later runs reuse the template. NIKA builds a new template when the pinned image, the pinned packages, `nika.sandbox.cpus`, or `nika.sandbox.memory` change.

## Model proxy and run records

Every model request of a sandboxed agent goes through a model proxy that NIKA starts on the host for the session. The sandbox reaches it at `http://host.docker.internal:<port>` on sbx, which only that sandbox may use, or through the session's tunnel on E2B and AgentENV. The proxy does three jobs:

- **Holds the credential.** The agent presents a random per-session token; the proxy rejects requests without it and sends the real API key or subscription login upstream.
- **Records every exchange.** It writes `model_calls.jsonl` in the session result directory: each request body exactly as the agent sent it and each response exactly as the agent received it (the raw event stream for streamed replies), with the phase, status, and token usage. The MCP gateway likewise writes every tool call, its arguments, and the result the agent received to `tool_calls.jsonl`. The sandbox cannot reach either file.
- **Enforces the step limit.** `max_steps` is the number of LLM requests each phase may make, counted at the proxy, so model calls of sub-agents count too. Token counting and model listing are not LLM requests. The proxy refuses the request past the limit, and NIKA stops the phase. A stopped diagnosis uses the latest assistant text the proxy recorded as its report, like an agent that reaches its limit itself; without any text the run fails.

The evaluator takes `steps`, token counts, and tool calls from these two records rather than from the agent-written `messages.jsonl`, which remains the transcript that `nika inspect` shows.

### Claude Code with custom providers

When you run `cli.claude` with `agent.provider: custom` (for example, a self-hosted vLLM server), the sandbox uses the Claude Code version that ships in the `claude` sandbox template. NIKA sets `DISABLE_AUTOUPDATER=1` for every `claude -p` call, so the version stays fixed for the whole run. The agent transcript (`messages.jsonl`) records the version as `claude_code_version` on the `init` event.

The proxy adapts Claude-family agents to the custom server:

| Adaptation | What NIKA does | Why |
|------------|----------------|-----|
| Context window | Reads `max_model_len` for `agent.model` from `{agent.base_url}/models`. Sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to that value and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to `min(32000, max_model_len / 8)`. | Claude Code assumes a 200k-token window for models it does not know. Without the real limit, it does not auto-compact before the server rejects a long request. |
| Message roles | Merges mid-conversation `role: "system"` turns into the adjacent user turn as `<system-reminder>` text and forwards all other traffic unchanged, including streamed responses. | Claude Code sends `role: "system"` entries inside `messages` from the first request on, for example the `Today's date is …` reminder. vLLM `/v1/messages` rejects them with HTTP 400 (`Input should be 'user' or 'assistant'`). |

If the server does not report `max_model_len`, Claude Code keeps its defaults. NIKA sends `NIKA_CUSTOM_API_KEY` with the `/models` request when you set it. The proxy reaches the endpoint from the NIKA host, so an endpoint on the host's `localhost` works. The proxy lives in [`src/nika/agents/sandbox/model_proxy.py`](../../src/nika/agents/sandbox/model_proxy.py).

## Architecture

The network environment, MCP gateway, and orchestration run on the host. The agent and its framework run in a sandbox and use the gateway to inspect the environment.

```
Host (NIKA orchestration)                 Sandbox (agent only)
├── Kathara / MCP gateway                 └── python -m agent.runtime <phase>
├── ground_truth.json                          ├── task.json, agent/ (+ added project, skills, wheels)
├── Host phase driver (freeze + advance)       ├── claude / codex CLIs, SDKs, or BYO frameworks
├── Model proxy (credentials, records, step limit)  └── environment tools: MCP HTTP only
└── sbx CLI or E2B API
```

Each task uses an ephemeral workspace, `$TMPDIR/agent-workspaces-<uid>/{agent_session_id}/`. sbx mounts it into the microVM at the same path; E2B copies it into the sandbox at the same path. It holds:

- `task.json`: the run settings and the session's MCP endpoints, read by [`agent.runtime`](../../src/agent/runtime/contract.py);
- `agent/`: the agent package, with no NIKA code, plus `project/` for an added agent;
- `.runtime_env.json`: `AGENT_WORKSPACE`, `PYTHONPATH`, and the standard model client variables (`OPENAI_*` or `ANTHROPIC_*`) pointing at the model proxy, with its per-session token.

The opaque `agent_session_id` is minted at env start and is what agents see in `task.json`, the MCP `X-Session-Id` header, sandbox names, and the workspace path. The human-readable host `session_id` / trial dirname (`trials/{case_key}__tNN/`) stays on the host, so benchmark case keys cannot leak as shortcuts. `ground_truth.json` and host `run.json` fields such as `problem_names` never enter the sandbox.

The agent writes its transcript in the workspace. The host copies new lines into its own `messages.jsonl` in the trial dir about once per second, so `nika inspect` and the benchmark dashboard can tail the run. The host drops any `diagnosis_frozen` event from the workspace copy and any event tagged with a phase other than the one running. After the run, NIKA copies `task.json` as `sandbox_manifest.json`, with its MCP access token redacted, and discards the workspace. NIKA never copies `submission.json` or `messages.jsonl` out of the workspace; the host MCP `submit()` tool writes the scored `submission.json` after validation.

### Phase boundary

During diagnosis, the agent cannot reach the submission context (fault ontology and resource catalog):

1. The MCP gateway's phase-advance endpoint requires a per-session secret that the host keeps in memory. NIKA never writes that secret into the sandbox environment, workspace, or MCP config. A request without that secret gets HTTP 403.
2. The agent runs diagnosis in one sandbox command, which writes `report.json` and exits. The host then records `diagnosis_frozen` from that report, advances the gateway, writes `submission_context.json` into the workspace, and starts the submission step. With unstructured submission, the host maps the report itself and the sandbox stays idle.

### How sandboxes stay isolated

Each agent run gets its own sandbox, workspace, and host MCP gateway on an ephemeral port. Every MCP request must carry the session's access token (`X-Session-Token`) as well as its `X-Session-Id`, so a client that knows or guesses a session ID still cannot use that session's gateway. Model credentials stay on the host: the sandbox receives a placeholder or a per-session proxy token.

**Docker Sandboxes:** each run uses its own sbx microVM (`agent-{agent_session_id}`). The sandbox network policy allows that session's `localhost:{port}` and blocks peer gateway ports. The host's global sbx rules stay as they are; NIKA denies each of them for the sandbox, so the agent reaches its MCP gateway and the model proxy but not, for example, GitHub, package indexes, or model APIs. Package indexes stay reachable only while NIKA installs the agent's packages. An added agent's declared [`egress`](../agents/agent-reference.md#custom-agent-registration) hosts stay reachable too. When a global rule covers such a host, NIKA keeps that whole rule, because sbx lets a wildcard deny override a narrower allow: declaring `api.openai.com` under a global `**.openai.com` rule also leaves the other `openai.com` subdomains reachable. Parallel benchmark runs (`--batch-size N`) keep up to N trials concurrent (sliding window) and use one subprocess per active trial, so gateways do not share a process. Those processes share one host `sandboxd`. NIKA starts that daemon with `upstream_proxy` when it is down, and does not restart it while it is up. If you change the proxy, stop the daemon when no sandboxes are running (`sbx daemon stop`) and start the next NIKA run.

**E2B:** sandboxes have no internet access. NIKA connects each sandbox to the host through a tunnel over E2B's command channel, which reaches only that session's MCP gateway and model proxy. Both listen on the host's loopback interface, except that a [remote lab host](remote.md) serves the gateway. An E2B sandbox expires `agent.timeout_sec` plus 30 minutes after it starts, or after 6.5 hours when `agent.timeout_sec` is `0`. NIKA removes it when the run ends, and `nika session close` removes it after an interrupted run.

**Sandbox boundary:** sandboxes never receive `nika/` source, host configuration, or the word NIKA. The `agent` package imports no host code, and [a contract test](../../tests/agent/test_agent_isolation_contract.py) fails if any file copied into a sandbox names NIKA. The live [sandbox agent tests](../../tests/agent/test_sandbox_agents.py) record every model request of an agent on a custom endpoint and check that neither those requests nor the sandbox's environment, home directory, or installed packages name it.

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

Or pass `--sandbox-cpus 2 --sandbox-memory 4g`. If the host has no KVM at all, use [E2B](#connect-to-e2b-cloud-or-another-e2b-compatible-service) instead.

### OAuth callback opens localhost over SSH

When you run `codex login` over SSH, the callback server runs on the remote host. Your browser opens `localhost` on your own computer, so sign-in cannot finish until you forward the callback port.

1. Keep the remote login command running. Find the callback port in the authorization URL's `redirect_uri` or the browser's failed callback address.
2. In another terminal on your own computer, create a tunnel to the same SSH server. For example, if the callback uses port `5411`:

   ```bash
   ssh -N -o ExitOnForwardFailure=yes \
     -L 127.0.0.1:5411:127.0.0.1:5411 ubuntu@YOUR_SSH_HOST
   ```

   Replace both port numbers with your callback port and use your SSH username and host.
3. Leave the tunnel open while completing the browser login. If the browser already reached the failed callback page, refresh that page after opening the tunnel.
4. When the remote command reports success, stop the tunnel with Ctrl+C.

If the login timed out, restart it and check the new callback port before signing in again. See [OpenAI's SSH callback guidance](https://learn.chatgpt.com/docs/auth#fallback-forward-the-localhost-callback-over-ssh).

### Model requests or Docker sign-in fail

Model requests leave from the NIKA host through its model proxy, so the host itself must reach the provider's API; sandboxes reach only the proxy.

With sbx, an outbound proxy for the sbx daemon is optional and off by default. If `sbx` cannot reach Docker's services, set it in `config/nika.yaml`:

```yaml
nika:
  sandbox:
    upstream_proxy: http://127.0.0.1:7890
```

Or pass `--sandbox-proxy` on the CLI.

NIKA also sets `HTTPS_PROXY` on host `sbx` subprocesses from that URL when `HTTPS_PROXY` is unset, so `sbx create` / `sbx exec` can fetch `https://login.docker.com/.well-known/jwks.json`. If Docker Hub token refresh still times out, confirm the proxy can reach `login.docker.com`, then run `sbx login`.

### AgentENV or E2B runs stop before the sandbox starts

- `connection refused` on `127.0.0.1:8800` means AgentENV is not running; check `docker ps -a --filter name=nika-agentenv` and `docker logs nika-agentenv`, or run `./scripts/install.sh --with-agentenv` again.

- `The e2b sandbox backend needs the E2B SDK` means the Python environment is out of date; run `uv sync`.
- `Set E2B_API_KEY in .../.env` means NIKA found no service key; see [Connect to E2B](#connect-to-e2b-cloud-or-another-e2b-compatible-service).
- `Could not build e2b template` means the service rejected the template build. The service must implement E2B's template build API. The next run removes the failed template and builds it again.
- `No API key or subscription login for provider` means the provider has neither an API key in `.env` nor a [subscription login](#subscriptions). This applies to every runtime.

The [agent test matrix](../development/testing.md#agent-tests-testsagent) lists sandbox unit, security, isolation, and end-to-end commands with their prerequisites.
