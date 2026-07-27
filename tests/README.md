# NIKA tests

Test layout mirrors product surfaces:

- `tests/agent/` → `src/agent/`
- `tests/nika/` → `src/nika/`
- `tests/benchmark/` → `nika benchmark` (YAML cases + `src/nika/workflows/benchmark/`)
- `tests/leaderboard/` → `nika leaderboard` (pack / validate / submit + release→submit E2E)
- `tests/support/` → shared helpers

## Layout

| Directory | Maps to | Purpose |
|-----------|---------|---------|
| `tests/agent/` | `src/agent/` | Per-agent unit tests and sandbox E2E |
| `tests/benchmark/` | `benchmark/` + workflow run/resume | Batch, resume, sandbox benchmark runs |
| `tests/leaderboard/` | `src/nika/workflows/leaderboard/` + CLI | Pack/validate/submit unit tests; mocked release→submit E2E; opt-in live GitHub PR (`NIKA_LEADERBOARD_E2E=1`) |
| `tests/nika/cli/` | `src/nika/cli/` | CLI smoke and import wiring |
| `tests/nika/workflows/integration/` | end-to-end session pipeline | env → inject → mock agent → close → metrics → summary |
| `tests/nika/problems/` | `src/nika/problems/` | Failure injection smoke tests (Kathara + Containerlab) |
| `tests/nika/net_env/` | `src/nika/net_env/` | Network environment deploy and topology checks |
| `tests/nika/service/` | `src/nika/service/` | Service-layer unit and live API smoke tests |
| `tests/nika/runtime/` | `src/nika/runtime/` | Runtime/backend unit tests and session index |
| `tests/nika/evaluator/` | `src/nika/evaluator/` | Rule-based scoring unit tests |
| `tests/support/` | — | Shared bases, prerequisites, and pipeline helpers |

## Shared support (`tests/support/`)

| Module | Purpose |
|--------|---------|
| `integration_base.py` | Session/env/failure workflow test bases |
| `integration_pipeline.py` | Ordered agent pipeline steps and credential probes |
| `prerequisites.py` | Docker, Containerlab, and image availability checks |
| `api_smoke.py` | Live API smoke mixin and JSON assertions |
| `net_env.py` | `verify_lab` assertion helpers |
| `kathara_api_base.py` | Shared Kathara API smoke test base class |

## Agent tests (`tests/agent/`)

Each module contains **unit tests** (no Docker) and, for LLM-backed agents, an **integration
pipeline** on `simple_bgp` / `link_down`:

| Module | Agent | Unit tests | Pipeline requires |
|--------|-------|------------|-------------------|
| `test_agent_config.py` | shared config | agent model/env resolution, judge env | — |
| `test_codex_cli.py` | `local_cli.codex_cli` | Codex TOML/display/worker config | Docker + Codex + OpenAI |
| `test_claude_cli.py` | `local_cli.claude_cli` | Claude JSON/display/auth helpers | Docker + Claude CLI |
| `test_langgraph.py` | `byo.langgraph` | — | Docker + `DEEPSEEK_API_KEY` |
| `test_mcp_agent.py` | `byo.mcp_agent` | — | Docker + `OPENAI_API_KEY` |
| `test_autogen.py` | `byo.autogen` | — | Docker + `DEEPSEEK_API_KEY` |
| `test_sade.py` | `community.sade` | SDK env + MCP adapter | Docker + `claude-agent-sdk` + Anthropic creds |
| `test_claude_sdk.py` | `sdk.claude_sdk` | SDK env + MCP adapter | Docker + `claude-agent-sdk` + Anthropic creds |
| `test_codex_sdk.py` | `sdk.codex_sdk` | auth/reasoning + MCP TOML | sbx + `openai-codex` + `OPENAI_API_KEY` |
| `test_mcp_server_selection.py` | shared MCP | diagnosis server selection | — |
| `test_sbx.py` | sandbox | sbx manager, credentials, proxy | — |
| `test_sandbox.py` | sandbox | manifest, redaction, SDK context | — |
| `test_sandbox_security.py` | sandbox | microVM security probe | sbx + Docker |
| `test_sandbox_isolation.py` | sandbox | distinct gateway ports + cross-sandbox MCP policy isolation | unit; sbx for peer-gateway probe |
| `test_sandbox_agents.py` | sandbox | five-agent E2E (`simple_bgp` / `link_down`) | sbx + Docker; Codex=`OPENAI_API_KEY`+`gpt-5-mini`, Claude/SADE=`DEEPSEEK_API_KEY`+`deepseek-v4-flash` |

Host-side pipeline classes in `test_*_cli.py`, `test_*_sdk.py`, and `test_sade.py` are skipped for live agent E2E; sandbox coverage lives in `test_sandbox_agents.py`.

```shell
# All agent tests (unit + pipeline; missing credentials skip pipeline only)
uv run pytest tests/agent/ -v

# Sandbox unit tests only (no sbx/lab)
uv run pytest tests/agent/test_sbx.py tests/agent/test_sandbox.py -v

# Security probe (sbx + MCP gateway)
uv run pytest tests/agent/test_sandbox_security.py -v

# Cross-sandbox isolation (unit + sbx peer-gateway probe)
uv run pytest tests/agent/test_sandbox_isolation.py -v

# Sandbox E2E (Codex needs OpenAI; Claude/SADE need DeepSeek)
uv run pytest tests/agent/test_sandbox_agents.py -v
```

## Benchmark tests (`tests/benchmark/`)

Covers `nika benchmark run` / resume — not YAML inject-param generation.
Batch mode requires explicit `--config` or `--release` (no bare default suite).

| Module | Purpose |
|--------|---------|
| `test_release.py` | Frozen `nika-bench@0.1.0` Dev/Test digests, isolation, job metadata; optional Docker smoke |
| `test_resume.py` | Resume/fingerprint unit tests (no Docker) |
| `test_trials.py` | Trial / release runs: cases×K trials, resume, agent_failed retain, isolation, `runtime/benchmark_runs` progress; Docker E2E mini-release run |
| `test_batch.py` | Parallel mock batch under shared `trials/` layout (`--config`, `n_trials=1`) |
| `test_sandbox_benchmark.py` | Claude + Codex sandbox single/parallel (`--batch-size 2`) |
| `helpers.py` | Load inject params from bundled benchmark YAML |

```shell
uv run pytest tests/benchmark/test_release.py -v
uv run pytest tests/benchmark/test_release.py -v -k DockerSmoke   # requires Docker
uv run pytest tests/benchmark/test_resume.py -v
uv run pytest tests/benchmark/test_trials.py -v
uv run pytest tests/benchmark/test_trials.py -v -k ReleaseRunE2E  # requires Docker
uv run pytest tests/benchmark/test_batch.py -v                 # requires Docker
uv run pytest tests/benchmark/test_sandbox_benchmark.py -v    # sbx + API key
```

## Leaderboard tests (`tests/leaderboard/`)

Covers `nika leaderboard template|pack|validate|submit` — no Docker for the default suite. Packs require a filled `metadata.yaml` + `README.md`. Docs: [`docs/leaderboard-submission.md`](../docs/leaderboard-submission.md).

| Module | Purpose |
|--------|---------|
| `test_pack_validate.py` | Schema/pack/validate unit tests (coverage, hashes, secrets, bad meta) |
| `test_submit_unit.py` | Mocked submit (direct push + fork path) |
| `test_e2e_release_pack.py` | Mocked `run_benchmark_from_release` → template → pack → validate |
| `test_e2e_release_submit.py` | Mocked release → pack → validate → submit (no network) |
| `test_e2e_submit_github.py` | Opt-in live draft PR + close (`NIKA_LEADERBOARD_E2E=1`) |

```shell
uv run pytest tests/leaderboard/ -v
uv run pytest tests/leaderboard/test_e2e_release_pack.py -v
uv run pytest tests/leaderboard/test_e2e_release_submit.py -v
NIKA_LEADERBOARD_E2E=1 uv run pytest tests/leaderboard/test_e2e_submit_github.py -v
```

## Sandbox verified status (local E2E, 2026-07-24)

| Area | Status | Notes |
|------|--------|-------|
| `test_sbx.py` / `test_sandbox.py` | **Passed** | Credentials, proxy, auth helpers |
| `test_sandbox_security.py` | **Passed** | microVM policy + MCP gateway isolation |
| `test_sandbox_isolation.py` | **Passed** | Distinct ports/names + unallowed peer gateway port blocked |
| Codex CLI / SDK (`test_sandbox_agents.py`, `OPENAI_API_KEY`, `gpt-5-mini`) | **Passed** | MCP tools → submission → eval |
| Claude CLI / SDK / SADE (`test_sandbox_agents.py`, DeepSeek, `deepseek-v4-flash`) | **Passed** | Anthropic-compatible DeepSeek |
| Benchmark single case (`test_sandbox_benchmark.py`, Claude/DeepSeek) | **Passed** | Same sandbox path as `nika agent run` |
| Benchmark Claude parallel (`--batch-size 2`) | **Passed** | Concurrent `link_down` + `link_flap` |
| Benchmark Codex parallel (`--batch-size 2`, `gpt-5-mini`) | **Passed** | Concurrent isolated sessions |
| Live OAuth / subscription E2E | **Not tested** | Unit coverage in `test_sbx.py` only |
| Official Anthropic API (non-DeepSeek) sandbox E2E | **Not tested** | Current E2E uses DeepSeek |
| Default Codex model `gpt-5.4-mini` | **Not verified** on restricted OpenAI projects | E2E uses `gpt-5-mini` when needed |

Prerequisites: `sbx login`, KVM, Docker/Kathara. Optional `NIKA_SANDBOX_UPSTREAM_PROXY` when Clash/TUN blocks LLM websockets. See [docs/agent-sandbox.md](../docs/agent-sandbox.md).

## Integration pipeline (`tests/nika/workflows/integration/`)

| Module | Purpose |
|--------|---------|
| `test_pipeline_kathara.py` | Kathara pipeline: env → inject → MCP → mock agent → close → metrics → summary |
| `test_pipeline_clab.py` | Containerlab min3clos pipeline (same steps) |

```shell
uv run pytest tests/nika/workflows/integration/test_pipeline_kathara.py -v   # requires Docker
uv run pytest tests/nika/workflows/integration/test_pipeline_clab.py -v      # requires containerlab + gnmic
```

## Service tests (`tests/nika/service/`)

MCP gateway wiring and MCP server tool delegation are **not** unit-tested in isolation;
they are covered by the workflow integration pipelines (`test_pipeline_kathara`,
`test_pipeline_clab`, agent pipelines) and live API smokes where applicable.

| Directory / module | Purpose |
|--------------------|---------|
| `pingmesh/test_parser.py` | Ping output parsing (`loss`, RTT, unreachable) |
| `pingmesh/test_endpoints.py` | Endpoint discovery and Containerlab data-plane IP selection |
| `pingmesh/test_engine.py` | Snapshot orchestration, anomaly summary, parameter bounds |
| `pingmesh/test_integration.py` | Live PingMesh MCP: healthy mesh → inject `link_down` → faulty mesh |
| `kathara/test_kathara_*.py` | Live Kathara API smoke tests (Docker) |
| `containerlab/test_containerlab_api.py` | Live Containerlab API smoke tests |
| `containerlab/test_srl_api.py` | SRL API parsing/script logic (mocked runtime) |

```shell
uv run pytest tests/nika/service/pingmesh/ -v
uv run pytest tests/nika/service/pingmesh/test_integration.py::KatharaPingMeshIntegrationTest -v
```

## Problem injection (`tests/nika/problems/`)

| Module | Backend |
|--------|---------|
| `test_kathara_failure_inject.py` | Kathara (Docker) |
| `test_clab_failure_inject.py` | Containerlab (skipped without `clab` on PATH) |

## Network environment (`tests/nika/net_env/`)

Deploy and `verify_lab` checks for Kathara and Containerlab scenarios.

## Runtime unit tests (`tests/nika/runtime/`)

Pure Python tests (no Docker): backend resolution, session index, system logger, etc.

```shell
uv run pytest tests/nika/runtime/ -v
```

## Evaluator unit tests (`tests/nika/evaluator/`)

```shell
uv run pytest tests/nika/evaluator/ -v
```

## Mock agent (test-only)

The **mock agent** (`src/agent/mock/mock_agent.py`) is a deterministic stand-in for
LLM-backed agents. It runs a fixed two-phase MCP tool sequence without API keys.

```shell
nika agent run -a mock -m mock-v1 -n 5 --session_id <id>
```

Mock runs expect perfect detection/RCA scores (`detection_score == 1.0`,
`rca_accuracy == 1.0`) because the agent reads ground truth from the session.
