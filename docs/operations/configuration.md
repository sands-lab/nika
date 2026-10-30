# Run configuration reference

This reference is for operators who configure NIKA runs. NIKA reads run settings from `config/nika.yaml` and credentials from the repository-root `.env`.

The installer creates these files when missing. For a manual checkout, create them before configuring a sandbox runtime:

```shell
cp config/nika.example.yaml config/nika.yaml
cp .env.example .env
uv run nika config show
```

To compare agents, copy your active configuration and change its agent settings:

```shell
cp config/nika.yaml config/cli.codex.yaml
uv run nika config set --run-config config/cli.codex.yaml \
  agent.type=cli.codex agent.provider=openai agent.model=gpt-5-mini
uv run nika agent run --run-config config/cli.codex.yaml --problem dc_clos_s_link_down
```

Each `--run-config` file is a complete configuration; it does not inherit `config/nika.yaml`. Copying the active file preserves your AgentENV, E2B, or sbx settings. Use per-agent templates as references when editing the copied file's `agent` section.

Tracked per-agent templates live next to `nika.example.yaml`: `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `cli.codex`, `cli.claude`, `cli.opencode`, `cli.dsh`, `sdk.codex_sdk`, and `sdk.claude_sdk` (each as `config/<agent>.example.yaml`).

`nika config show` validates the selected YAML file and prints the effective configuration without credentials. Use `--run-config PATH` or `NIKA_RUN_CONFIG` to select a different operations file.

Persist important agent settings without hand-editing YAML:

```shell
uv run nika config set agent.provider=custom agent.model=qwen2.5:7b \
  agent.base_url=http://localhost:11434/v1
```

`nika config set` accepts any scalar key that `nika config show` prints, such as `benchmark.batch_size` or `nika.lab.deploy_attempts`. It overwrites the value on the existing line, or adds the key under its section, and leaves comments and other lines unchanged. It rejects unknown keys and validates the file before it writes. Edit the YAML file for lists and maps.

For a single run, override the same fields on `nika agent run` / `nika benchmark run` with `-p`, `-m`, and `--base-url` (CLI wins over YAML).

## Configuration precedence

NIKA resolves values in this order:

1. CLI flags
2. The selected YAML file
3. Defaults in [`run_config/schema.py`](../../src/nika/run_config/schema.py)

The tracked [`config/nika.example.yaml`](../../config/nika.example.yaml) is the full platform reference (`agent:` / `nika:` / `benchmark:`) with a default `byo.langgraph` profile. The per-agent `config/<agent>.example.yaml` files provide agent settings to use in your profile. Missing keys fall back to schema defaults, including `sbx` for the sandbox backend. A run must set `agent.model` (or pass `-m/--model`).

Relative result paths resolve from the repository root. NIKA rejects unknown YAML keys and values outside the validation constraints below.

## `agent` settings

| Key | Schema default | Meaning and constraints |
| --- | --- | --- |
| `agent.type` | `byo.langgraph` | Agent registry name. Run `uv run nika agent list` for available names. |
| `agent.provider` | `openai` | Provider name. The selected agent must support it. |
| `agent.model` | `null` | Canonical model id for the active agent type. |
| `agent.base_url` | `null` | Required for `provider: custom`. Use the OpenAI-style API root, usually ending in `/v1` (for example `http://localhost:8000/v1`), not the bare host and port. Other providers ignore it; set `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, or `DEEPSEEK_BASE_URL` in `.env` to route them through a gateway. Set via YAML, `nika config set agent.base_url=...`, or `--base-url` on `agent run` / `benchmark run`. |
| `agent.max_steps` | `20` | Host-enforced limit on successful LLM generation requests per phase for every sandboxed agent, including sub-agent requests. Provider failures do not consume the budget. Must be at least `1`. See [Step limit](../agents/agent-reference.md#step-limit). |
| `agent.timeout_sec` | `1800` | Wall-clock budget in seconds for one agent run (diagnosis plus submission). Applies to every agent type. When it expires, NIKA stops the agent and cleans up its sandbox. Benchmark batch runs record the trial as counted `agent_failed`, or as `success` when the agent already wrote a valid `submission.json`. Keep it below `benchmark.case_timeout_sec`. Set `0` to disable it. |
| `agent.enable_skills` | `true` | Load the shared skill library for Claude, Codex, OpenCode, and DeepSeek Harness agents. |
| `agent.submit_reject_limit` | `5` | Max consecutive `submit()` validation failures. After the last one, `submit()` refuses every later call for the session, including valid ones. Set `0` to disable the cap. |
| `agent.reasoning_effort` | `null` | Reasoning effort. When unset, built-in harnesses use `medium` and `community.sade` and added agents get none. Accepted levels depend on the agent; see [Reasoning effort](../agents/agent-reference.md#reasoning-effort). |
| `agent.max_tokens` | `8192` | Output-token cap per model response. BYO agents pass it to the model client; `cli.claude` and `sdk.claude_sdk` set `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. `cli.codex`, `sdk.codex_sdk`, and `community.sade` do not apply it and record `null`. Benchmark runs record it in the run identity. Must be at least `1`. |
| `agent.temperature`, `agent.top_p`, `agent.seed` | `null` | Sampling parameters for `byo.langgraph` and `byo.mcp_agent`. Unset values are omitted from the request. `temperature` must be at least `0`, `top_p` in `(0, 1]`. Anthropic and DeepSeek clients ignore `seed`. Benchmark runs and `run.json` record the explicit values, or `"server default"`, as `sampling`. |
| `agent.llm.timeout_sec` | `480` | LLM request timeout used by the `byo.langgraph` model factory. Must be non-negative. With streaming it bounds the wait between chunks, not the whole response. |
| `agent.llm.max_retries` | `2` | LLM retries used by the `byo.langgraph` model factory. Must be non-negative. |
| `agent.llm.stream` | `true` | Stream LLM responses in the `byo.langgraph` model factory. Set `false` for OpenAI-compatible servers that do not support streaming or `stream_options`. |
| `agent.submission.mode` | `structured` | How the frozen report becomes the scored answer. `structured`: the agent's own model calls `submit`. `unstructured`: the model below maps the report for every agent type. See [submission modes](../agents/agent-reference.md#shared-run-contract). Benchmark runs record `agent.submission` in the run identity; only `structured` runs can be packed for the leaderboard. |
| `agent.submission.provider` | `null` | `unstructured` only, required. Provider of the mapping model; credentials come from `.env` as for `agent.provider`. |
| `agent.submission.model` | `null` | `unstructured` only, required. Mapping model id. Its output is capped by `agent.max_tokens`, also for agents that ignore that setting. |
| `agent.submission.base_url` | `null` | `unstructured` only. Endpoint for `provider: custom`; defaults to `agent.base_url`. |
| `agent.submission.samples` | `1` | `unstructured` only. Independent mappings of the report; NIKA submits the most frequent valid answer (ties go to the earliest). Each mapping gets one corrective retry when its answer is invalid. |
| `agent.submission.temperature` | `null` | `unstructured` only. Sampling temperature of the mapping model; `null` keeps the provider default, which is `0` for `custom`. |
| `agent.access.role` | `default` | Diagnosis access role for the run. `--role` on `agent run` / `benchmark run` overrides it. The role must exist in `agent.access.roles`. Benchmark runs record it in the run identity. |
| `agent.access.roles.<role>.tools` | `["*"]` | MCP tools the agent may call during diagnosis. `*` allows every tool. Submission always allows only `submit`. |
| `agent.access.roles.<role>.node_roles` | `["*"]` | Scenario node roles that node-targeted tools may address. `*` allows every role. |
| `agent.access.roles.<role>.node_ids` | `[]` | Extra node names allowed regardless of `node_roles`. |

Model resolution order: `-m/--model`, then `agent.model`.

| Agent | Providers |
| --- | --- |
| `byo.langgraph` | `openai`, `anthropic`, `deepseek`, `custom` |
| `byo.mcp_agent` | `openai`, `anthropic`, `deepseek`, `custom` |
| `byo.autogen` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.opencode` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.codex` | `openai`, `deepseek`, `custom` |
| `sdk.codex_sdk` | `openai`, `deepseek`, `custom` |
| `cli.dsh` | `openai`, `deepseek`, `custom` |
| `cli.claude` | `anthropic`, `deepseek`, `custom` |
| `sdk.claude_sdk` | `anthropic`, `deepseek`, `custom` |
| `community.sade` | `anthropic`, `deepseek`, `custom` |

Provider credentials belong in `.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, or optional `NIKA_CUSTOM_API_KEY`. NIKA keeps them on the host; sandboxes receive a model-proxy token or subscription placeholder. Codex and Claude harnesses can also use subscription login on either sandbox runtime. See [Authentication](agent-sandbox.md#authentication) for setup and supported agents.

E2B service credentials are separate: the host reads `E2B_API_KEY` and optional `E2B_DOMAIN` to connect to the sandbox service. See [Connect to E2B](agent-sandbox.md#connect-to-e2b-cloud-or-another-e2b-compatible-service).

Leaderboard trajectory submit reads `HF_TOKEN` from `.env` / the environment. See [leaderboard submission](../benchmarks/leaderboard-submission.md).

## `nika` settings

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.result_dir` | `results` | Parent directory for session and benchmark artifacts. |
| `nika.remote.enabled` | `false` | Route lab lifecycle calls to the remote control plane. |
| `nika.remote.url` | `null` | Remote control-plane base URL. Required when remote mode is enabled. |
| `nika.remote.artifact_root` | `null` | Reserved path carried in the remote client configuration. Current workflows do not consume it. |
| `nika.sandbox.backend` | `sbx` | Sandbox runtime: `sbx` (Docker Sandboxes on the host) or `e2b` (E2B or an E2B-compatible service). See [Choose a sandbox runtime](agent-sandbox.md#choose-a-sandbox-runtime). |
| `nika.sandbox.url` | `null` | `e2b` only: address of an E2B-compatible service that serves its API and sandboxes at one address, such as AgentENV. `null` uses `E2B_DOMAIN` from `.env`, else E2B cloud. |
| `nika.sandbox.keep` | `false` | Keep the sandbox after the agent exits. An E2B sandbox stays until `nika session close` or the end of its lifetime. |
| `nika.sandbox.cpus` | `2` | vCPU count per sandbox. `null` lets sbx give each sandbox every host vCPU, or E2B use the service default. |
| `nika.sandbox.memory` | `4g` | Memory limit per sandbox. `null` lets sbx use half of host memory, or E2B use the service default. |
| `nika.sandbox.offline_sdk_wheels` | `true` | `sbx` only: stage cached SDK wheels for SDK and SADE sandboxes. Set `false` to install packages from PyPI inside the sandbox. |
| `nika.sandbox.upstream_proxy` | `null` | `sbx` only: proxy used by the shared `sandboxd` process and host `sbx` commands. |
| `nika.judge.provider` | `openai` | Provider used by `nika eval judge` when the CLI does not override it. |
| `nika.judge.model` | `gpt-5-mini` | Judge model used when the CLI does not override it. |
| `nika.k8s.access` | `auto` | `auto` and `mcp` register the Kubernetes MCP server. `kubectl_only` skips it. |
| `nika.k8s.apiserver` | `null` | Optional Kubernetes API server override for the host-side client. |

### State directory

NIKA stores `runtime/` and `.nika_cache/` at the root of the main Git checkout (the parent of `git rev-parse --git-common-dir`), so every worktree of the same repository shares sessions and caches. When the source tree is not a Git checkout, they live at the repository root.

### Lab lifecycle settings

| Key | Default | Meaning and constraints |
| --- | ---: | --- |
| `nika.lab.deploy_attempts` | `3` | Kathara deployment attempts. Must be at least `1`. |
| `nika.lab.deploy_ready_timeout_sec` | `90` | Time allowed for all Kathara machines to enter the running state. Must be non-negative. |
| `nika.lab.deploy_settle_sec` | `5` | Delay after deploy on Kathara and Containerlab. NIKA skips it when the environment defines `startup_verify_lab()` or overrides `verify_lab()`. Must be non-negative. |
| `nika.lab.undeploy_verify_timeout_sec` | `30` | Time allowed for Kathara containers to disappear after undeploy. Must be non-negative. |
| `nika.lab.ready_max_wait_sec` | `180` | Default startup-verification polling window for scenarios that implement `verify_lab()`. A scenario-level `VERIFY_MAX_WAIT_SEC` overrides it. Must be non-negative. |
| `nika.lab.ready_retry_delay_sec` | `5` | Default delay between startup-verification attempts. A scenario-level `VERIFY_RETRY_DELAY_SEC` overrides it. Must be non-negative. |
| `nika.lab.failure_verify_max_attempts` | `3` | Calls to `verify_fault()` before injection fails. Must be at least `1`. |
| `nika.lab.failure_verify_retry_delay_sec` | `5` | Delay between fault-verification attempts. Must be non-negative. |
| `nika.lab.containerlab_max_workers` | `2` | Passed to `clab deploy --max-workers`. Limits concurrent node create and virtual-wire workers. Must be at least `1`. Lower this on memory-tight hosts when Containerlab labs OOM during deploy. |

### MCP settings

| Key | Default | Meaning and constraints |
| --- | ---: | --- |
| `nika.mcp.read_timeout_sec` | `120` | MCP request timeout for the shared LangGraph, AutoGen, and mcp-agent clients, and the per-call `tool_timeout_sec` for the Codex agents (`cli.codex`, `sdk.codex_sdk`). Non-positive values disable the shared clients' timeout and leave `tool_timeout_sec` unset, so Codex uses its own default. |
| `nika.mcp.gateway_host` | `127.0.0.1` | Host address for the session MCP gateway. |
| `nika.mcp.gateway_port` | `0` | Gateway port. `0` selects a free port; negative values are invalid. |
| `nika.mcp.tool_output_max_chars` | `10000` | Characters of MCP tool text returned to the agent. Longer output keeps the first and last halves and marks how many characters were elided (mini-swe-agent style), with a warning to use `head`/`tail`/`grep` or redirect to a file. `0` disables truncation. Negative values are invalid. |

### Static validation

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.static_validation.enabled` | `false` | Run the optional Batfish verifier before deployment for supported ISP Kathara FRR scenarios. |
| `nika.static_validation.batfish_heap` | `8g` | Java heap (`<n>m` or `<n>g`) for the Batfish container NIKA creates. Remove the existing container to apply a new value. |

The CLI flag `--static-validation` overrides this setting for one run. Use `--no-static-validation` to force the runtime-only path.

### Runtime validation

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.runtime_validation.depth` | `light` | `light` polls `startup_verify_lab` when present (bounded readiness). `full` always polls `verify_lab` (deep connectivity and contract intents where implemented). |
| `nika.runtime_validation.failure_effect` | `false` | After failure inject, compare healthy vs faulty contract evidence (deep runtime ± Batfish when artifacts exist). Off by default; does not affect `verify_fault`. |

Default production and benchmark paths use light runtime checks without Batfish or failure-effect validation. For the deep maintainer path, set `depth: full`, `failure_effect: true`, and optionally `static_validation.enabled: true`.

### Monitoring

`nika observability on` applies the sampling and remote-write settings to the monitoring stack; run it again after changing them. The `share` settings apply to each new run link: changing them, or running `on` again, leaves links that are already open as they are. See [monitor benchmarks and lab resources](resource-monitoring.md) and, for revoking open links, [share a run's dashboard](resource-monitoring.md#share-a-runs-dashboard).

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.metrics.scrape_interval` | `15s` | How often Prometheus scrapes and cAdvisor samples, from `5s` to `60s`. |
| `nika.metrics.remote_write.enabled` | `false` | Also push NIKA's series to `nika.metrics.remote_write.url`. Requires the URL and the token. |
| `nika.metrics.remote_write.url` | `null` | Remote-write endpoint that also receives NIKA's series, such as Grafana Cloud's Prometheus. The token is `NIKA_METRICS_REMOTE_WRITE_TOKEN` in `.env`. |
| `nika.metrics.remote_write.username` | `null` | Basic-auth username, such as the Grafana Cloud instance ID. Null sends the token as a bearer token. |
| `nika.metrics.share.enabled` | `false` | Give each benchmark run a public dashboard link through a Cloudflare Quick Tunnel. |
| `nika.metrics.share.ttl_days` | `7` | Days until a run's link stops working, from 1 to 30. |
| `nika.metrics.share.allowed_emails` | `[]` | Addresses (or patterns such as `*@example.com`) allowed to view a link after an emailed one-time code. Empty: anyone with the link. |

## `benchmark` settings

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `benchmark.release` | `null` | Frozen release selected when `--release` is absent. |
| `benchmark.split` | `null` | Release split selected when `--split` is absent. |
| `benchmark.batch_size` | `1` | Max concurrent light trials (sliding window). Must be at least `1`. |
| `benchmark.heavy_batch_size` | `1` | Max concurrent heavy trials: Containerlab, k8s/llmd/XRd, and topo_size `l` cases. Must be at least `1`. Heavy and light trials never overlap; with `1`, each heavy trial runs alone on the host. When a heavy trial is next in the queue, NIKA stops starting light trials, waits for running trials to finish, then starts heavy trials. |
| `benchmark.case_timeout_sec` | `2400` | Hard wall-clock limit per trial. Set `0` to disable it. |
| `benchmark.continue_on_error` | `false` | Continue the batch after a failed trial. |
| `benchmark.retry_passes` | `0` | Additional passes over failed or incomplete trials. Must be non-negative. |
| `benchmark.resume` | `true` | Reuse completed trial slots in the result directory. |
| `benchmark.session_tag` | `null` | Optional tag added to benchmark session identifiers. |

Benchmark case lists and injection parameters remain in `--release` data or a `--config` case matrix. See the [benchmark configuration reference](../benchmarks/benchmark-configuration.md).

NIKA ignores old operational variables in `.env`; move any needed values to `config/nika.yaml`. `NIKA_RUN_CONFIG` selects the YAML file rather than configuring a run value.
