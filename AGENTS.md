# NIKA

NIKA is a platform for managing, generating, and running executable benchmarks for network troubleshooting agents. Python dependencies and commands use `uv`.

## Development rules

* Read the relevant code and tests before modifying behavior.
* Follow existing abstractions and make the smallest change needed.
* Reuse existing scenario, runtime, traffic, telemetry, failure, and session infrastructure.
* Preserve benchmark reproducibility, ground truth, telemetry semantics, and backend neutrality.
* Add regression tests for behavior changes when practical.
* Do not weaken tests to make changes pass.

## Architecture

* CLI parsing and presentation: `src/nika/cli/commands/`.
* Reusable workflows: `src/nika/workflows/`.
* Lab lifecycle and sessions: `src/nika/runtime/`; backend implementations under `runtime/kathara/` and `runtime/containerlab/`.
* Device/backend APIs: `src/nika/service/`; keep shared and backend-specific APIs separated.
* MCP implementations belong under the corresponding MCP service modules. Agents should reuse shared tools.
* Keep registries importable without optional emulator or agent dependencies; preserve lazy loading.
* Register scenarios in `src/nika/net_env/net_env_pool.py`.
* Remote lab control belongs in `src/nika/remote/`.

## Scenarios and failures

* Prefer realistic network architectures, protocols, workloads, telemetry, and documented failure modes.
* Keep scenarios minimal while retaining what is required to reproduce and diagnose their failures.
* New failures subclass `ProblemBase`, define typed `Params`, `failure_domain`, and `root_cause_name`; add `symptom_desc` when needed.
* Follow the taxonomy in `docs/failures.md`.
* Failure registry: `src/nika/problems/registry.py`. Authoring base: `base.py`. RCA schema/inventory/materialize: `rca/`. Submission owner-kind policy: `ownership.py`.
* Cross-domain failure helpers belong in `src/nika/problems/support/`; concrete registered failures do not.
* Failures should represent root causes rather than symptoms.
* Avoid telemetry or metadata that leaks ground truth.

## Benchmark contracts

Scenario/failure compatibility uses `TAGS` subset matching, plus optional `COMPATIBLE_COLUMNS` on the failure when tags alone would match too broadly.

Treat changes to `TAGS`, `COMPATIBLE_COLUMNS`, registries, compatibility, root-cause names, ground truth, or benchmark cases as benchmark contract changes.

After compatibility changes:

```bash
uv run python scripts/render_coverage_matrix.py --write-docs
```

After benchmark-case changes:

```bash
uv run python benchmark/generate_benchmark.py
uv run python scripts/render_coverage_matrix.py --write-docs
```

Review generated diffs. Do not manually edit generated benchmark YAML unless explicitly required. Treat `benchmark/releases/` as frozen publication data.

## Agents

* Agents implement `agent.protocols.TroubleshootingAgent` and register in `agent/registry.py`.
* Community agents belong under `src/agent/community/<name>/`.
* Agents should use shared NIKA tools instead of duplicating tool behavior.
* Use the deterministic `mock` agent for tests that should not require external credentials.

## Configuration and state

* Configuration precedence: CLI flags → `config/nika.yaml` → defaults.
* Credentials belong in the repository-root `.env`; never commit secrets.
* Runtime state belongs under `runtime/`; experiment artifacts under `results/{session_id}/`.
* Reuse `nika.utils.session_resolve` for session selection.

## Testing

* Run focused tests before broader suites.
* Exercise CLI paths when changing configuration, paths, or session behavior.
* Use isolated result directories and session IDs for integration tests.
* Distinguish unavailable external prerequisites from code failures.

**Any test that creates a NIKA session or external resource must clean it up before exiting, including on failure.**

Use fixtures or `try/finally` so cleanup always runs. Close/wipe sessions through NIKA lifecycle APIs or commands.

A test must not leave behind resources it created, including Docker containers/networks, Kathara or Containerlab labs, Kubernetes resources, subprocesses, or network namespaces.

Cleanup must target only test-owned resources. Never perform system-wide Docker, emulator, or runtime cleanup.

## Verification

Run relevant tests, then:

```bash
uv run ruff format .
uv run ruff check .
```

Before completion:

* Confirm intended tests pass.
* Confirm test-created sessions and external resources are gone.
* Review the repository diff for unintended changes.
* Update documentation when externally visible behavior or benchmark contracts change.

An integration test is not complete if its assertions pass but its resources remain.

## Operational safety

* Do not bulk-delete `runtime/` or `results/`.
* Prefer `nika session close` or `nika session wipe` over manual emulator/Docker cleanup.
* Never remove unrelated Docker, Kathara, Containerlab, or Kubernetes resources.
* Preserve scenario configs, startup files, P4 programs, manifests, topology/data files, and traffic datasets unless explicitly modifying them.

## Documentation

* `README.md`: user-facing introduction.
* `docs/README.md`: documentation index.
* Keep `docs/failures.md` and `docs/benchmark-configuration.md` synchronized with implementation and generated coverage.
