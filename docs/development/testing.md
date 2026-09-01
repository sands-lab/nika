# Testing guide

This guide is for contributors who need to choose and run the smallest relevant NIKA test suite. The test layout mirrors the product packages:

Source: [`tests/`](../../tests/) contains the suites, and [`tests/support/`](../../tests/support/) contains shared integration helpers.

- `tests/agent/` → `src/agent/`
- `tests/nika/` → `src/nika/`
- `tests/benchmark/` → `nika benchmark` (YAML cases + `src/nika/workflows/benchmark/`)
- `tests/leaderboard/` → `nika leaderboard` (submit packs/validates + release→submit E2E)
- `tests/support/` → shared helpers

## Layout

| Directory | Maps to | Purpose |
|-----------|---------|---------|
| `tests/agent/` | `src/agent/` | Per-agent unit tests and sandbox E2E |
| `tests/benchmark/` | `benchmark/` + workflow run/resume | Batch, release, trials, sandbox benchmark runs; runner YAML load contracts |
| `tests/leaderboard/` | `src/nika/workflows/leaderboard/` + CLI | Pack/validate/submit unit tests; mocked release→submit E2E; opt-in live GitHub PR (`NIKA_LEADERBOARD_E2E=1`) |
| `tests/nika/cli/` | `src/nika/cli/` | CLI smoke and import wiring |
| `tests/nika/workflows/integration/` | end-to-end session pipeline | env → inject → mock agent → close → metrics → summary |
| `tests/nika/problems/` | `src/nika/problems/` | Failure injection smoke tests (Kathara + Containerlab) |
| `tests/nika/net_env/` | `src/nika/net_env/` | Network environment deploy and topology checks |
| `tests/nika/service/` | `src/nika/service/` | Service-layer unit and live API smoke tests |
| `tests/nika/runtime/` | `src/nika/runtime/` | Runtime/backend unit tests and session index |
| `tests/nika/evaluator/` | `src/nika/evaluator/` | Rule-based scoring unit tests |
| `tests/support/` | Not applicable | Shared bases, prerequisites, and pipeline helpers |

Local lab integration tests expect lab extras installed (`uv sync --extra labs --group dev`). Core/agent unit tests should run without Kathara/Containerlab packages.

Packet capture inspect tests and live inspect operations require `tshark` on lab nodes (`nika/base` and `nika/frr` images). Rebuild those images after Dockerfile changes. Capture uses `tcpdump` or `dumpcap` when present on the node.

## Pytest markers

Markers are registered in `pyproject.toml` and auto-applied from path conventions in `tests/conftest.py`:

| Marker | Purpose |
|--------|---------|
| `unit` | Fast tests without Docker |
| `contract` | Registry, benchmark YAML, schema, artifact contracts |
| `integration` | Env deploy, failure inject, traffic, MCP smoke |
| `sandbox` | Sandbox isolation and security |
| `e2e` | Full mock-agent pipeline and benchmark flows |
| `live` | Real LLM / GitHub / Batfish (credentials required) |

```shell
# Fast local smoke (no Docker)
uv run pytest -m "unit or contract" -q

# Emulator / lab integration (Docker / containerlab)
uv run pytest -m integration -q

# Full mock-agent flows
uv run pytest -m "e2e and not live" -q

# Sandbox isolation (serial; sbx + Docker)
uv run pytest -m sandbox -q

# Real LLM / live services (credentials required)
uv run pytest -m live -q
```

Run these tiers locally; there is no GitHub Actions workflow for them yet (lab backends and credentials are not available on shared CI runners).

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

Each module contains **unit tests** (no Docker) and, for LLM-backed agents, an **integration pipeline** on a test-only BGP fixture with `link_down`:

| Module | Agent | Unit tests | Pipeline requires |
|--------|-------|------------|-------------------|
| `test_agent_config.py` | shared config | agent model/env resolution, judge env | None |
| `test_codex_cli.py` | `cli.codex` | Codex TOML/display/worker config | Docker + Codex + OpenAI |
| `test_claude_cli.py` | `cli.claude` | Claude JSON/display/auth helpers | Docker + Claude CLI |
| `test_langgraph.py` | `byo.langgraph` | None | Docker + `DEEPSEEK_API_KEY` |
| `test_mcp_agent.py` | `byo.mcp_agent` | None | Docker + `OPENAI_API_KEY` |
| `test_autogen.py` | `byo.autogen` | None | Docker + `DEEPSEEK_API_KEY` |
| `test_sade.py` | `community.sade` | SDK env + MCP adapter | Docker + `claude-agent-sdk` + Anthropic creds |
| `test_claude_sdk.py` | `sdk.claude_sdk` | SDK env + MCP adapter | Docker + `claude-agent-sdk` + Anthropic creds |
| `test_codex_sdk.py` | `sdk.codex_sdk` | auth/reasoning + MCP TOML | sbx + `openai-codex` + `OPENAI_API_KEY` |
| `test_mcp_read_timeout.py` | shared MCP | client read-timeout configuration | None |
| `test_sbx.py` | sandbox | sbx manager, credentials, proxy | None |
| `test_sandbox.py` | sandbox | manifest, redaction, SDK context | None |
| `test_sandbox_security.py` | sandbox | microVM security probe | sbx + Docker |
| `test_sandbox_isolation.py` | sandbox | distinct gateway ports + cross-sandbox MCP policy isolation | unit; sbx for peer-gateway probe |
| `test_sandbox_agents.py` | sandbox | five-agent E2E (test BGP fixture / `link_down`) | sbx + Docker; Codex=`OPENAI_API_KEY`+`gpt-5-mini`, Claude/SADE=`DEEPSEEK_API_KEY`+`deepseek-v4-flash` |

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

Covers `nika benchmark run` / resume / release orchestration and runner YAML load contracts (`alias`, `migrate`, `task_label`). Offline inject-param generation, ISP option/symptom targeting, and healthy-case rules live next to their packages (`tests/nika/problems/`, `tests/nika/net_env/isp/`, `tests/nika/workflows/`). Batch mode requires explicit `--config` or `--release` (no bare default suite).

| Module | Purpose |
|--------|---------|
| `test_release.py` | Deprecated `0.1.0` rejection; freeze/preflight/job metadata on mini releases |
| `test_trials.py` | Trial / release runs: cases×K trials, resume, agent_failed retain, isolation, `runtime/benchmark_runs` progress; Docker E2E mini-release run |
| `test_batch.py` | Parallel mock batch under shared `trials/` layout (`--config`, `n_trials=1`) |
| `test_sandbox_benchmark.py` | Claude + Codex sandbox single/parallel (`--batch-size 2`) |
| `test_curated_release_contract.py` | Test-only curated rows from `0.2.0` `test` split: subset contract, mock pack/validate, timeout+continue |
| `test_curated_release_e2e.py` | Live Docker + `byo.langgraph` / DeepSeek on the curated subset (batch, resume, summary, pack/validate) |
| `test_alias_load.py` | Reject legacy scenario aliases / invalid workload columns |
| `test_migrate.py` | Benchmark YAML migrate → `root_causes` |
| `test_task_label.py` | Compound task label format/parse |
| `helpers.py` | Load inject params from bundled benchmark YAML |
| `curated.py` / `fixtures/curated_0_2_0_test.yaml` | Curated 0.2.0 test-split rows (tests only; not a published release) |

Related (moved out of this directory):

| Module | Purpose |
|--------|---------|
| `tests/nika/problems/test_inject_resolve.py` | Offline inject-target resolve/validate for YAML generation |
| `tests/nika/net_env/isp/test_isp_options.py` | ISP deploy-option selection + row normalize/fingerprint |
| `tests/nika/net_env/isp/test_isp_bgp_symptom.py` | ISP BGP inject symptom host / probe target attachment |
| `tests/nika/workflows/test_healthy_cases.py` | Healthy (no-fault) case normalize + selected YAML coverage |

```shell
uv run pytest tests/benchmark/test_release.py -v
uv run pytest tests/benchmark/test_release.py -v -k DockerSmoke   # requires Docker
uv run pytest tests/benchmark/test_trials.py -v
uv run pytest tests/benchmark/test_trials.py -v -k ReleaseRunE2E  # requires Docker
uv run pytest tests/benchmark/test_batch.py -v                 # requires Docker
uv run pytest tests/benchmark/test_sandbox_benchmark.py -v    # sbx + API key
uv run pytest tests/benchmark/test_curated_release_contract.py -v
# Live curated path (serial; needs Docker + DEEPSEEK_API_KEY; avoid parallel k8s/llmd suites)
uv run pytest tests/benchmark/test_curated_release_e2e.py -m live -v
```

## Leaderboard tests (`tests/leaderboard/`)

Covers `nika leaderboard template|submit` (submit packs and validates before opening PRs). The default suite does not require Docker. Submissions require a filled `metadata.yaml` and `README.md`. See the [leaderboard submission guide](../benchmarks/leaderboard-submission.md).

| Module | Purpose |
|--------|---------|
| `test_pack_validate.py` | Schema/pack/validate unit tests (coverage, hashes, secrets, bad meta) |
| `test_submit_unit.py` | Mocked submit (direct push + fork path) |
| `test_e2e_release_pack.py` | Mocked `run_benchmark_from_release` → template → pack → validate |
| `test_e2e_release_submit.py` | Mocked release → submit (pack + validate + PRs, no network) |
| `test_e2e_submit_github.py` | Opt-in live draft PR + close (`NIKA_LEADERBOARD_E2E=1`) |

```shell
uv run pytest tests/leaderboard/ -v
uv run pytest tests/leaderboard/test_e2e_release_pack.py -v
uv run pytest tests/leaderboard/test_e2e_release_submit.py -v
NIKA_LEADERBOARD_E2E=1 uv run pytest tests/leaderboard/test_e2e_submit_github.py -v
```

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

MCP gateway wiring and MCP server tool delegation are **not** unit-tested in isolation; they are covered by the workflow integration pipelines (`test_pipeline_kathara`, `test_pipeline_clab`, agent pipelines) and live API smokes where applicable.

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

| Module | Backend | Purpose |
|--------|---------|---------|
| `test_failure_inject_contract.py` | Kathara + Containerlab | **Verify-only**: parametrized inject + ground-truth via `verify_fault` |
| `test_symptom_contracts.py` | none | Symptom contract registry + gray probe unit coverage |
| `test_sdn_l3_clos_failure_compat.py` | Kathara | TAGS-compatible failures + `evaluate_symptom` |
| `test_p4_dc_fabric_failure_compat.py` | Kathara | P4 fabric inject/verify samples |
| `test_p4_dc_gateway_p4runtime_failure_compat.py` | Kathara | Shared P4Runtime failure suite |
| `test_link_capacity_bottleneck.py` | Kathara | VDE proxy TBF + iperf symptom on `dc_clos` |
| `test_mtu_mismatch.py` | Kathara | Path MTU / Frag Needed probes on `dc_clos` |
| `test_pmtu_blackhole_combo.py` | Kathara | MTU + Frag Needed filter black-hole combo |
| `test_load_balancer_overload.py` | Kathara | VIP overload: verify_fault + `evaluate_symptom` |
| `test_failure_e2e.py` | Kathara + Containerlab | Parametrized inject, artifact verification, symptom evaluation, and recovery; core cases include sender resource contention |
| `test_web_dos_attack.py` | Kathara | Web DoS: separate verify-only and symptom suites |

```shell
uv run pytest tests/nika/problems/test_symptom_contracts.py -v
uv run pytest tests/nika/problems/test_failure_inject_contract.py -v
```

Artifact `verify_fault` gates `nika failure inject`. Symptom checks use the unified test API `tests.support.symptom.evaluate_symptom` (per-failure contracts + custom handlers). User workflows must not import that package. Gray/statistical failures use heavier probes inside `evaluate_symptom`; `probe="artifact_only"` skips network probes when impact is nondeterministic (BGP ACL).

## Network environment (`tests/nika/net_env/`)

Startup uses fast `startup_verify_lab()`; full healthy baseline checks run in tests via `tests.support.scenario_evaluate.evaluate_scenario` (calls `verify_lab()`). User workflows must not import that module.

| Module | Backend | Purpose |
| --- | --- | --- |
| `test_kathara_verify.py` | FakeRuntime + Kathara | Unit tests for startup/full verify; integration deploy + `evaluate_scenario` |
| `test_scenario_e2e.py` | Kathara, Containerlab, k8s | Parametrized Docker E2E for all registered scenarios |
| `test_clab_min3clos_verify.py` | Containerlab | min3clos deploy + full verify |
| `isp/test_isp_integration.py` | Kathara + Containerlab | ISP matrix; includes full `verify_lab` |

```shell
uv run pytest tests/nika/net_env/test_kathara_verify.py -q -k 'not Integration'
uv run pytest tests/nika/net_env/test_scenario_e2e.py -q
```

Integration tests close only the test-tag session they create (`close_session(session_id=...)`). Do not use `nika session wipe` while other sessions are running.

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

The **mock agent** (`src/agent/mock/mock_agent.py`) is a deterministic stand-in for LLM-backed agents. It runs a fixed two-phase MCP tool sequence without API keys.

```shell
nika agent run -a mock -m mock-v1 -n 5 --session_id <id>
```

Mock runs expect perfect detection/RCA scores (`detection_score == 1.0`, `rca_accuracy == 1.0`) because the agent reads ground truth from the session.
