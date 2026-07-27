# Agent Instructions

## Project Snapshot

- NIKA is a Python 3.12 network troubleshooting benchmark and orchestration platform.
- It deploys Kathara or Containerlab labs, injects reproducible network faults, runs troubleshooting agents, and evaluates their submissions.
- The package uses a `src/` layout. The console entry point is `nika = nika.cli.main:main`.
- Dependencies are managed with `uv`; `.env` is loaded from the repository root by `src/nika/config.py`.

## Repository Map

- `src/nika/cli/`: Typer command groups for `session`, `env`, `failure`, `exec`, `agent`, `eval`, `benchmark`, `leaderboard`, and `traffic`.
- `src/nika/workflows/`: command orchestration split by domain (`agent/`, `benchmark/`, `env/`, `eval/`, `exec/`, `failure/`, `leaderboard/`, `session/`).
- `src/nika/runtime/`: backend-neutral runtime contracts plus Kathara and Containerlab runtime implementations.
- `src/nika/net_env/`: registered network lab definitions under `kathara/` and `containerlab/`.
- `src/nika/problems/`: injectable `ProblemBase` fault classes grouped by root-cause category and registered through `prob_pool`.
- `src/nika/service/`: lab APIs, backend adapters, MCP gateway/server code, shell helpers, and pingmesh telemetry.
- `src/nika/generator/`: fault and traffic generation utilities.
- `src/nika/evaluator/`: metrics, submission schemas, trace parsing, summaries, and LLM judge support.
- `src/agent/`: troubleshooting agents, agent registry, shared protocols, SDK/local-CLI integrations, sandbox runner, and shared skills.
- `benchmark/`: benchmark YAML cases and regeneration script.
- `tests/`: unit and integration tests mirroring `src/`, with shared fixtures in `tests/support/`.

## Common Commands

- Install dependencies: `uv sync`
- Install SDK-agent extras: `uv sync --extra sdk --prerelease=allow`
- Run the CLI during development: `uv run nika --help`
- List available scenarios/problems/agents: `uv run nika env list`, `uv run nika failure list`, `uv run nika agent list`
- Regenerate benchmark YAML: `uv run python benchmark/generate_benchmark.py`
- Leaderboard pack/validate: `uv run nika leaderboard --help` (see `docs/leaderboard-submission.md`)
- Format Python code: `uv run ruff format .`
- Lint Python code: `uv run ruff check .`

## Testing Commands

- Install dev dependencies (includes pytest): `uv sync --group dev`
- Run all tests: `uv run pytest`
- Run a focused path: `uv run pytest tests/nika/runtime/ -v`
- Run agent tests: `uv run pytest tests/agent/ -v`
- Run leaderboard tests: `uv run pytest tests/leaderboard/ -v`
- Prefer focused tests near the changed behavior before broader suites.
- Docker, Kathara, Containerlab, local CLIs, or API credentials are required for many integration and agent tests; report unavailable prerequisites instead of treating them as code failures.

## Architecture Rules

- Keep CLI parsing and option handling in `src/nika/cli/commands/`; put reusable behavior in `src/nika/workflows/` or lower-level modules.
- Session-scoped workflows should accept or resolve `session_id` consistently with the CLI auto-selection behavior: use the sole running session when unambiguous, otherwise require `--session_id`.
- Runtime state belongs under `runtime/`; experiment artifacts belong under `results/{session_id}/`.
- Relative result paths must resolve from the repository root, matching `resolve_results_root()`.
- Backend-specific lab lifecycle behavior belongs in `src/nika/runtime/kathara/` or `src/nika/runtime/containerlab/`; shared runtime contracts belong in `src/nika/runtime/base.py`, `spec.py`, `meta.py`, or helpers.
- Backend-specific low-level APIs belong under `src/nika/service/kathara/` or `src/nika/service/containerlab/`; backend-neutral lab adapters belong under `src/nika/service/lab/`.
- MCP tool behavior belongs under `src/nika/service/mcp_server/` or `src/nika/service/mcp_gateway/`; avoid duplicating tool behavior in agent implementations.
- New Kathara network scenarios belong under `src/nika/net_env/kathara/`; Containerlab scenarios belong under `src/nika/net_env/containerlab/`. Register new scenarios in the environment pool.
- New injectable problems belong under `src/nika/problems/`, subclass `ProblemBase`, set `root_cause_category` and `root_cause_name`, optionally set `symptom_desc`, and define typed `Params`. Registration in `prob_pool` keys on `root_cause_name`; `META` is auto-built from class variables.

## Agent System Rules

- Agent implementations live under `src/agent/` and implement the shared troubleshooting contract in `agent.protocols`.
- Production agent ids include `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `local_cli.codex_cli`, `local_cli.claude_cli`, `community.sade`, `sdk.claude_sdk`, and `sdk.codex_sdk`.
- The `mock` agent is deterministic and test-only; prefer it for no-credential pipeline tests.
- Agent runs should write standard artifacts such as `messages.jsonl` and `submission.json` in the session result directory.
- Shared agent skills live under `src/agent/skills/`; helpers live in `agent.utils.skills`. See `docs/agent-skills.md`.
- SADE keeps its own skill library under `src/agent/community/sade/.claude/`.
- Sandbox execution uses Docker Sandboxes (`sbx`) under `src/agent/sandbox/` with official `codex` / `claude` / `shell` templates. Behavior: `docs/agent-sandbox.md`; tests: `tests/README.md`.

## Formatting and Linting

- Python code must be formatted with Ruff using `uv run ruff format .`.
- Run `uv run ruff check .` before submitting changes when practical.
- Prefer Ruff auto-fixes only for mechanical cleanup; avoid unrelated style churn outside the task scope.
- Keep documentation concise and point detailed usage to `README.md`, `src/nika/cli/README.md`, `docs/custom-agents.md`, `docs/agent-skills.md`, `docs/creating-benchmark-tasks.md`, and `docs/leaderboard-submission.md`.

## Testing Guidance

- For pure Python changes, use targeted `pytest` commands before broader suites.
- For CLI behavior, test through the CLI path when practical because config resolution, repository-root path resolution, and session selection are part of the behavior.
- For benchmark resume or artifact logic, verify against a temporary or isolated `--result_dir`.
- Do not assume Docker/Kathara/Containerlab integration tests can run in every environment; report skipped or unavailable checks clearly.

## Environment and Secrets

- Use `.env.example` as the template; do not commit real API keys or local credentials.
- CLI flags override `.env` values.
- Common agent variables include `NIKA_AGENT_TYPE`, `NIKA_MAX_STEPS`, `NIKA_MODEL`, and agent-specific model/provider variables.
- Claude and Codex local CLI agents require their respective CLIs on `PATH` plus configured authentication.
- SDK agents require the `sdk` optional dependency group.

## Operational Cautions

- Do not delete `runtime/` or `results/` broadly unless the user explicitly asks; they may contain active sessions or experiment outputs.
- Use `nika session close` or `nika session wipe` for lab cleanup instead of manually removing Docker/Kathara/Containerlab state.
- Avoid changing generated benchmark YAML by hand unless the task is specifically about benchmark cases.
- Preserve existing lab config files, startup files, P4 programs, Kubernetes manifests, and Containerlab topology files unless the change directly targets them.
- Network tests can be slow and environment-sensitive; keep verification commands specific and explain any external prerequisites.
