# NIKA

NIKA is a Python 3.12 platform for deploying Kathara or Containerlab network scenarios, injecting failures, running troubleshooting agents, and evaluating their submissions. Dependencies use `uv`.

## Architecture boundaries

- Keep Typer parsing and presentation in `src/nika/cli/commands/`. Put behavior that must work outside a CLI callback in `src/nika/workflows/` or a lower-level module.
- Use `src/nika/runtime/` for lab lifecycle and session operations. Keep shared contracts in `base.py`, `spec.py`, and `meta.py`; put backend implementations under `runtime/kathara/` or `runtime/containerlab/`.
- Use `src/nika/service/` for device and backend APIs. Backend-neutral adapters belong in `service/lab/`; backend-specific APIs belong in `service/kathara/` or `service/containerlab/`.
- Keep MCP behavior in `service/mcp_server/`, `service/mcp_gateway/`, or the Kubernetes MCP server. Agent implementations should call shared tools instead of copying tool behavior.
- Keep registries importable with core dependencies. Do not import emulator packages or agent SDKs at module scope; preserve lazy loading and explicit extra checks.
- Register scenarios in `src/nika/net_env/net_env_pool.py`. Put Kathara and Containerlab implementations in their matching backend directories.
- New failures subclass `ProblemBase`, define typed `Params`, set `root_cause_category` and `root_cause_name`, and add `symptom_desc` when the name does not describe the symptom. Use the six-category taxonomy in `docs/failures.md`. `prob_pool.py` discovers concrete classes and keys them by `root_cause_name`.
- Scenario and failure compatibility uses `TAGS` subset matching. Treat a `TAGS` or registry change as a benchmark contract change; regenerate the working matrices and review the diff.
- Agents implement `agent.protocols.TroubleshootingAgent` and register their CLI name in `agent/registry.py`. Community implementations live under `src/agent/community/<name>/`; their operator references live under `docs/agents/community/`.
- Keep the optional remote lab control plane in `src/nika/remote/`. MCP `remote_proxy` and leaderboard transport serve different purposes.

## Configuration and state

- Configuration precedence is CLI flags, then `config/nika.yaml`, then code defaults. `--run-config` and `NIKA_RUN_CONFIG` select another operations file.
- Keep credentials in the repository-root `.env`; use `.env.example` as the template. Do not commit credentials.
- Resolve relative result paths from the repository root through `resolve_results_root()`. Runtime state belongs under `runtime/`; experiment artifacts belong under `results/{session_id}/`.
- Session-scoped operations may select the sole running session. If several sessions run, require `--session_id` and reuse `nika.utils.session_resolve`.
- The `mock` agent is deterministic and test-only. Use it for pipeline tests that should not need credentials.

## Verification

- Run focused tests near the changed subsystem before broader suites. Locate them by subsystem name under `tests/`.
- Exercise the CLI path when config resolution, repository-root paths, or session selection affect behavior.
- Use a temporary or isolated `--result_dir` for benchmark resume and artifact tests.
- Format Python with `uv run ruff format .` and lint with `uv run ruff check .`.
- Docker, Kathara, Containerlab, `clab`, `gnmic`, Kubernetes, local agent CLIs, and API credentials gate some integration tests. Distinguish missing prerequisites from code failures.

## Operational safety

- Do not delete `runtime/` or `results/` in bulk; they can contain active sessions and experiment outputs.
- Use `nika session close` or `nika session wipe` for lab cleanup instead of removing emulator or Docker state by hand.
- Do not edit generated benchmark YAML by hand unless the task targets benchmark cases. Regenerate matrices with `uv run python benchmark/generate_benchmark.py`.
- Treat `benchmark/releases/` as frozen publication data. Modify it only when the task creates or updates a release.
- Preserve lab configs, startup files, P4 programs, Kubernetes manifests, SNDlib data, and Containerlab topology files unless the task targets them.

## Documentation

- Keep `README.md` as the user-facing introduction. Start from `docs/README.md` for detailed references and update its index when adding or moving a page.
- Keep Markdown prose on one source line and let the renderer wrap it.
