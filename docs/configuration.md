# Run configuration reference

NIKA reads run settings from `config/nika.yaml` and credentials from the repository-root `.env`.

Copy the tracked templates for a new checkout:

```shell
cp config/nika.example.yaml config/nika.yaml
cp .env.example .env
uv run nika config show
```

`nika config show` validates the selected YAML file and prints the effective configuration without credentials. Use `--run-config PATH` or `NIKA_RUN_CONFIG` to select a different operations file.

## Configuration precedence

NIKA resolves values in this order:

1. CLI flags
2. The selected YAML file
3. Defaults in [`run_config/schema.py`](../src/nika/run_config/schema.py)

The tracked [`config/nika.example.yaml`](../config/nika.example.yaml) supplies usable model choices. The schema leaves `agent.model` and every `agent.models.*` field unset, so a run without the template must pass `-m/--model` or define a model in another YAML file.

Relative result paths resolve from the repository root. NIKA rejects unknown YAML keys and values outside the validation constraints below.

## `nika` settings

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.result_dir` | `results` | Parent directory for session and benchmark artifacts. |
| `nika.enable_skills` | `true` | Load the shared skill library for Claude and Codex agents. |
| `nika.remote.enabled` | `false` | Route lab lifecycle calls to the remote control plane. |
| `nika.remote.url` | `null` | Remote control-plane base URL. Required when remote mode is enabled. |
| `nika.remote.artifact_root` | `null` | Reserved path carried in the remote client configuration. Current workflows do not consume it. |
| `nika.sandbox.keep` | `false` | Keep the Docker Sandbox after the agent exits. |
| `nika.sandbox.cpus` | `null` | Optional `sbx` CPU limit. |
| `nika.sandbox.memory` | `null` | Optional `sbx` memory limit, such as `8g`. |
| `nika.sandbox.offline_sdk_wheels` | `false` | Stage cached SDK wheels for SDK and SADE sandboxes. |
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
| `nika.lab.deploy_settle_sec` | `5` | Delay after Kathara reports all machines running. Must be non-negative. |
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

### Static validation

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `nika.static_validation.enabled` | `false` | Run the optional Batfish verifier before deployment for supported ISP Kathara FRR scenarios. Live runtime verification remains enabled for normal startup. |

The CLI flag `--static-validation` overrides this setting for one run. Use `--no-static-validation` to force the runtime-only path.

## `agent` settings

| Key | Schema default | Meaning and constraints |
| --- | --- | --- |
| `agent.type` | `byo.langgraph` | Agent registry name. Run `uv run nika agent list` for available names. |
| `agent.provider` | `openai` | Provider name. The selected agent must support it. |
| `agent.model` | `null` | Model override shared by all agent types. Takes precedence over `agent.models.*`. |
| `agent.max_steps` | `20` | Step or turn limit passed to agents that support it. Must be at least `1`. |
| `agent.reasoning_effort` | `null` | Optional reasoning effort. Accepted levels depend on the agent. |
| `agent.custom.base_url` | `null` | Required for `provider: custom`. Also overrides the endpoint for `openai` or `anthropic`. |
| `agent.custom.model` | `null` | Final model fallback when `provider: custom`. |
| `agent.llm.timeout_sec` | `300` | LLM request timeout used by the `byo.langgraph` model factory. Must be non-negative. |
| `agent.llm.max_retries` | `2` | LLM retries used by the `byo.langgraph` model factory. Must be non-negative. |

`agent.models` stores one model per implementation. Model resolution uses `-m/--model`, then `agent.model`, then the implementation-specific field, then `agent.custom.model` for the custom provider.

| Agent | Model field | Providers |
| --- | --- | --- |
| `byo.langgraph` | `agent.models.langgraph` | `openai`, `anthropic`, `deepseek`, `custom` |
| `byo.mcp_agent` | `agent.models.mcp_agent` | `openai`, `anthropic`, `deepseek`, `custom` |
| `byo.autogen` | `agent.models.autogen` | `openai`, `anthropic`, `deepseek`, `custom` |
| `cli.codex` | `agent.models.codex` | `openai`, `deepseek`, `custom` |
| `sdk.codex_sdk` | `agent.models.codex_sdk`, then `agent.models.codex` | `openai`, `deepseek`, `custom` |
| `cli.claude` | `agent.models.claude` | `anthropic`, `deepseek`, `custom` |
| `sdk.claude_sdk` | `agent.models.claude_sdk`, then `agent.models.claude` | `anthropic`, `deepseek`, `custom` |
| `community.sade` | `agent.models.sade`, then `agent.models.claude` | `anthropic`, `deepseek`, `custom` |

Provider credentials belong in `.env`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, or optional `NIKA_CUSTOM_API_KEY`. NIKA maps credentials for the selected provider into the agent process or sandbox.

## `benchmark` settings

| Key | Default | Meaning and constraints |
| --- | --- | --- |
| `benchmark.release` | `null` | Frozen release selected when `--release` is absent. |
| `benchmark.split` | `null` | Release split selected when `--split` is absent. |
| `benchmark.batch_size` | `1` | Concurrent trials. Must be at least `1`. |
| `benchmark.case_timeout_sec` | `2400` | Hard wall-clock limit per trial. Set `0` to disable it. |
| `benchmark.continue_on_error` | `false` | Continue the batch after a failed trial. |
| `benchmark.retry_passes` | `0` | Additional passes over failed or incomplete trials. Must be non-negative. |
| `benchmark.resume` | `true` | Reuse completed trial slots in the result directory. |
| `benchmark.session_tag` | `null` | Optional tag added to benchmark session identifiers. |

Benchmark case lists and injection parameters remain in `--release` data or a `--config` case matrix. See the [benchmark configuration reference](benchmark-configuration.md).

## Migrate operational `.env` keys

Existing installations can convert legacy operational environment variables:

```shell
uv run nika config migrate
uv run nika config migrate --write-env
```

The command prints the proposed YAML and asks before writing. `--write-env` also backs up `.env` to `.env.bak`, then keeps recognized credentials in `.env`. Pass `-y` to skip both confirmations.

NIKA ignores legacy operational variables during normal runs and prints one warning listing the detected keys. `NIKA_RUN_CONFIG` remains supported because it selects the YAML file rather than configuring a run value.
