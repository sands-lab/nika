# Run configuration reference

This reference is for operators who configure NIKA runs. NIKA reads run settings from `config/nika.yaml` and credentials from the repository-root `.env`.

Copy the tracked templates for a new checkout:

```shell
cp config/nika.example.yaml config/nika.yaml
cp .env.example .env
uv run nika config show
```

To compare agents, copy a per-agent template and pass `--run-config`:

```shell
cp config/cli.codex.example.yaml config/cli.codex.yaml
# edit provider / model in that file if needed
uv run nika agent run --run-config config/cli.codex.yaml --problem dc_clos_s_link_down
```

Tracked per-agent templates live next to `nika.example.yaml`: `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `cli.codex`, `cli.claude`, `sdk.codex_sdk`, and `sdk.claude_sdk` (each as `config/<agent>.example.yaml`).

`nika config show` validates the selected YAML file and prints the effective configuration without credentials. Use `--run-config PATH` or `NIKA_RUN_CONFIG` to select a different operations file.

Persist important agent settings without hand-editing YAML:

```shell
uv run nika config set agent.provider=custom agent.model=qwen2.5:7b \
  agent.custom.base_url=http://localhost:11434/v1
```

`nika config set` accepts these 11 keys: `agent.type`, `agent.provider`, `agent.model`, `agent.max_steps`, `agent.timeout_sec`, `agent.reasoning_effort`, `agent.custom.base_url`, `agent.enable_skills`, `nika.result_dir`, `nika.judge.provider`, and `nika.judge.model`. Edit the YAML file for every other key. The command rejects any other key and validates the merged file before it writes.

For a single run, override the same fields on `nika agent run` / `nika benchmark run` with `-p`, `-m`, and `--base-url` (CLI wins over YAML).

## Configuration precedence

NIKA resolves values in this order:

1. CLI flags
2. The selected YAML file
3. Defaults in [`run_config/schema.py`](../../src/nika/run_config/schema.py)

The tracked [`config/nika.example.yaml`](../../config/nika.example.yaml) is the full platform reference (`agent:` / `nika:` / `benchmark:`) with a default `byo.langgraph` profile. Prefer the per-agent `config/<agent>.example.yaml` files when you want one ready profile per harness; missing keys fall back to schema defaults. A run must set `agent.model` (or pass `-m/--model`).

Relative result paths resolve from the repository root. NIKA rejects unknown YAML keys and values outside the validation constraints below.

## Top-level settings

| Key | Schema default | Meaning and constraints |
| --- | --- | --- |
| `version` | `1` | Run configuration format version. |

## `agent` settings

| Key | Schema default | Meaning and constraints |
| --- | --- | --- |
| `agent.type` | `byo.langgraph` | Agent registry name. Run `uv run nika agent list` for available names. |
| `agent.provider` | `openai` | Provider name. The selected agent must support it. |
| `agent.model` | `null` | Canonical model id for the active agent type. |
| `agent.max_steps` | `20` | Max LLM turns per phase for agents that support it (same unit as eval `steps` / `llm_end`). `byo.langgraph` enforces it with model-call limits and `cli.claude` with `claude --max-turns`. `cli.codex` and `sdk.codex_sdk` have no turn limit. Must be at least `1`. |
| `agent.timeout_sec` | `1800` | Wall-clock budget in seconds for one agent run (diagnosis plus submission). Applies to every agent type. When it expires, NIKA stops the agent and cleans up its sandbox. Benchmark batch runs record the trial as counted `agent_failed`, or as `success` when the agent already wrote a valid `submission.json`. Keep it below `benchmark.case_timeout_sec`. Set `0` to disable it. |
| `agent.enable_skills` | `true` | Load the shared skill library for Claude and Codex agents. |
| `agent.submit_reject_limit` | `5` | Max consecutive `submit()` validation failures. After the last one, `submit()` refuses every later call for the session, including valid ones. Set `0` to disable the cap. |
| `agent.reasoning_effort` | `null` | Optional reasoning effort. Accepted levels depend on the agent. |
| `agent.custom.base_url` | `null` | Required for `provider: custom`. Also overrides the endpoint for `openai` or `anthropic`. Set via YAML, `nika config set agent.custom.base_url=...`, or `--base-url` on `agent run` / `benchmark run`. |
| `agent.custom.model` | `null` | Deprecated fallback when `provider: custom` and `agent.model` is unset. |
| `agent.llm.timeout_sec` | `480` | LLM request timeout used by the `byo.langgraph` model factory. Must be non-negative. |
| `agent.llm.max_retries` | `2` | LLM retries used by the `byo.langgraph` model factory. Must be non-negative. |
| `agent.models.<agent>` | `null` | Deprecated per-agent model ids. Sub-keys: `langgraph`, `mcp_agent`, `autogen`, `codex`, `codex_sdk`, `claude`, `claude_sdk`, `sade`. NIKA reads one only when `-m` and `agent.model` are unset, and prints a deprecation warning. |
| `agent.access.role` | `default` | Diagnosis access role for the run. `--role` on `agent run` / `benchmark run` overrides it. The role must exist in `agent.access.roles`. Benchmark runs record it in the run identity. |
| `agent.access.roles.<role>.tools` | `["*"]` | MCP tools the agent may call during diagnosis. `*` allows every tool. Submission always allows only `submit`. |
| `agent.access.roles.<role>.node_roles` | `["*"]` | Scenario node roles that node-targeted tools may address. `*` allows every role. |
| `agent.access.roles.<role>.node_ids` | `[]` | Extra node names allowed regardless of `node_roles`. |

Model resolution order: `-m/--model`, then `agent.model`, then `agent.custom.model` when `provider: custom`, then deprecated `agent.models.*` (emits a warning).

`agent.models` is deprecated. Set `agent.model` in one YAML file per run profile. To compare models, copy the YAML or pass `-m`.

| Agent | Providers |
| --- | --- |
| `byo.langgraph` | `openai`, `anthropic`, `deepseek`, `custom` |
| `byo.mcp_agent` | `openai`, `anthropic`, `deepseek`, `custom` |
| `byo.autogen` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.codex` | `openai`, `deepseek`, `custom` |
| `sdk.codex_sdk` | `openai`, `deepseek`, `custom` |
| `cli.claude` | `anthropic`, `deepseek`, `custom` |
| `sdk.claude_sdk` | `anthropic`, `deepseek`, `custom` |
| `community.sade` | `anthropic`, `deepseek`, `custom` |

Provider credentials belong in `.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, or optional `NIKA_CUSTOM_API_KEY`. NIKA maps credentials for the selected provider into the agent process or sandbox.

Leaderboard trajectory submit reads `HF_TOKEN` from `.env` / the environment. See [leaderboard submission](../benchmarks/leaderboard-submission.md).

## `nika` settings

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.result_dir` | `results` | Parent directory for session and benchmark artifacts. |
| `nika.remote.enabled` | `false` | Route lab lifecycle calls to the remote control plane. |
| `nika.remote.url` | `null` | Remote control-plane base URL. Required when remote mode is enabled. |
| `nika.remote.artifact_root` | `null` | Reserved path carried in the remote client configuration. Current workflows do not consume it. |
| `nika.sandbox.keep` | `false` | Keep the Docker Sandbox after the agent exits. |
| `nika.sandbox.cpus` | `null` | Optional `sbx` CPU limit. |
| `nika.sandbox.memory` | `null` | Optional `sbx` memory limit, such as `8g`. |
| `nika.sandbox.offline_sdk_wheels` | `true` | Stage cached SDK wheels for SDK and SADE sandboxes. Set `false` to install packages from PyPI inside the sandbox. |
| `nika.sandbox.upstream_proxy` | `null` | Proxy used by the shared `sandboxd` process and host `sbx` commands. |
| `nika.observability.langfuse_enabled` | `false` | Enable Langfuse callbacks for `byo.langgraph`. |
| `nika.observability.langfuse_host` | `https://cloud.langfuse.com` | Langfuse endpoint. Credentials remain in `.env`. |
| `nika.judge.provider` | `openai` | Provider used by `nika eval judge` when the CLI does not override it. |
| `nika.judge.model` | `gpt-5-mini` | Judge model used when the CLI does not override it. |
| `nika.k8s.access` | `auto` | `auto` and `mcp` register the Kubernetes MCP server. `kubectl_only` skips it. |
| `nika.k8s.apiserver` | `null` | Optional Kubernetes API server override for the host-side client. |

### Lab lifecycle settings

| Key | Default | Meaning and constraints |
| --- | ---: | --- |
| `nika.lab.deploy_attempts` | `3` | Kathara deployment attempts. Must be at least `1`. |
| `nika.lab.deploy_ready_timeout_sec` | `90` | Time allowed for all Kathara machines to enter the running state. Must be non-negative. |
| `nika.lab.deploy_settle_sec` | `5` | Delay after deploy on Kathara and Containerlab. NIKA applies it only to scenarios without a readiness verifier. Must be non-negative. |
| `nika.lab.undeploy_verify_timeout_sec` | `30` | Time allowed for Kathara containers to disappear after undeploy. Must be non-negative. |
| `nika.lab.ready_max_wait_sec` | `180` | Default startup-verification polling window for scenarios that implement `verify_lab()`. A scenario-level `VERIFY_MAX_WAIT_SEC` overrides it. Must be non-negative. |
| `nika.lab.ready_retry_delay_sec` | `5` | Default delay between startup-verification attempts. A scenario-level `VERIFY_RETRY_DELAY_SEC` overrides it. Must be non-negative. |
| `nika.lab.failure_verify_max_attempts` | `3` | Calls to `verify_fault()` before injection fails. Must be at least `1`. |
| `nika.lab.failure_verify_retry_delay_sec` | `5` | Delay between fault-verification attempts. Must be non-negative. |

### MCP settings

| Key | Default | Meaning and constraints |
| --- | ---: | --- |
| `nika.mcp.read_timeout_sec` | `120` | MCP request timeout used by the shared LangGraph, AutoGen, and mcp-agent clients. Non-positive values disable it. |
| `nika.mcp.gateway_host` | `127.0.0.1` | Host address for the session MCP gateway. |
| `nika.mcp.gateway_port` | `0` | Gateway port. `0` selects a free port; negative values are invalid. |
| `nika.mcp.tool_output_max_chars` | `16384` | Characters of MCP tool output returned to the agent. Longer output is truncated with a banner that tells the agent to re-call with `full=true`. `0` disables truncation. Negative values are invalid. |
| `nika.mcp.tool_output_full_max_chars` | `100000` | Cap for tool output when the agent passes `full=true`. `0` removes the cap. Negative values are invalid. |

### Static validation

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.static_validation.enabled` | `false` | Run the optional Batfish verifier before deployment for supported ISP Kathara FRR scenarios. |

The CLI flag `--static-validation` overrides this setting for one run. Use `--no-static-validation` to force the runtime-only path.

### Runtime validation

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.runtime_validation.depth` | `light` | `light` polls `startup_verify_lab` when present (bounded readiness). `full` always polls `verify_lab` (deep connectivity and contract intents where implemented). |
| `nika.runtime_validation.failure_effect` | `false` | After failure inject, compare healthy vs faulty contract evidence (deep runtime ± Batfish when artifacts exist). Off by default; does not affect `verify_fault`. |

Default production and benchmark paths use light runtime checks without Batfish or failure-effect validation. For the deep maintainer path, set `depth: full`, `failure_effect: true`, and optionally `static_validation.enabled: true`.

## `benchmark` settings

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `benchmark.release` | `null` | Frozen release selected when `--release` is absent. |
| `benchmark.split` | `null` | Release split selected when `--split` is absent. |
| `benchmark.batch_size` | `1` | Max concurrent trials (sliding window). Must be at least `1`. |
| `benchmark.serialize_heavy` | `true` | When true, Containerlab, k8s/llmd/XRd, and topo_size `l` cases run exclusively (at most one such trial, and no peer sessions of any class) even if `batch_size` is higher. Light s/m Kathara trials still use the full `batch_size` when no exclusive lab is active. When an exclusive trial is next in the queue, NIKA stops starting light trials, waits for running trials to finish, then starts the exclusive trial. |
| `benchmark.case_timeout_sec` | `2400` | Hard wall-clock limit per trial. Set `0` to disable it. |
| `benchmark.continue_on_error` | `false` | Continue the batch after a failed trial. |
| `benchmark.retry_passes` | `0` | Additional passes over failed or incomplete trials. Must be non-negative. |
| `benchmark.resume` | `true` | Reuse completed trial slots in the result directory. |
| `benchmark.session_tag` | `null` | Optional tag added to benchmark session identifiers. |

Benchmark case lists and injection parameters remain in `--release` data or a `--config` case matrix. See the [benchmark configuration reference](../benchmarks/benchmark-configuration.md).

## Migrate operational `.env` keys

Existing installations can convert legacy operational environment variables:

```shell
uv run nika config migrate
uv run nika config migrate --write-env
```

The command prints the proposed YAML and asks before writing. `--write-env` also backs up `.env` to `.env.bak`, then keeps recognized credentials in `.env`. Pass `-y` to skip both confirmations.

NIKA ignores legacy operational variables during normal runs and prints one warning listing the detected keys. `NIKA_RUN_CONFIG` remains supported because it selects the YAML file rather than configuring a run value.
