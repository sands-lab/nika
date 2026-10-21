# NIKA

NIKA is a platform for generating and running executable benchmarks for network troubleshooting agents. A benchmark is a set of tasks; each task combines a network environment, a network incident, and a network traffic pattern. Python dependencies and commands use `uv`.

Start from the [documentation index](docs/README.md). It maps each contract to its implementation and lists the registry commands (`uv run nika env list`, `failure list`, `agent list`) to check before you change a catalog or count.

## Development rules

* Read the relevant code before you change behavior, and make the smallest change that works.
* Reuse existing NIKA abstractions instead of building parallel implementations. Add helpers, wrappers, or abstractions only when they have clear reuse value.
* Preserve benchmark reproducibility, ground truth, telemetry semantics, and backend neutrality.
* Do not weaken tests to make a change pass.
* Write human-facing documentation with the [NIKA documentation skill](.agents/skills/nika-docs/SKILL.md), and use the terms in the [glossary](GLOSSARY.md).
* When the user repeatedly asks for the same reusable coding or workflow practice, add it to the matching section of this file. Do not record task-specific decisions.

## Commit and pull request messages

Commit subjects and pull request titles follow Conventional Commits: `type(scope): imperative summary`, where the type is one of `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`, `ci`, `build`, or `revert`. The [`commit-msg` hook](scripts/git-hooks/commit-msg) rejects other subjects (`./scripts/install.sh` enables it), and the `Commit messages` CI check applies the same rule to every pull request commit and title.

* The pull request title is the Conventional Commit line without a PR number. The merge commit subject is that line followed by `(#<PR number>)`.
* The body is a short summary followed by `Fixes #<issue>` (or `Closes` or `Resolves`) so the merge closes the issue. Use one keyword per issue. When the work only contributes to an issue, write `Refs #<issue>` instead; it links the issue without closing it. Omit both when there is no issue.

## Labels

Label every issue and pull request you open with the repository's existing labels (`gh label list`); do not create new ones. Use `bug` for defects, `enhancement` for features, and `documentation` for docs. Add `bug-from-audit` for bugs that could bias agent diagnosis, `agent-ready` for well-scoped issues an agent can implement, and `needs-human` for issues that need a human design decision. Pass them with `--label` on `gh issue create` and `gh pr create`.

## Leaderboard submissions

Submit with the [leaderboard submission skill](.agents/skills/nika-leaderboard-submit/SKILL.md). Name package directories `YYYYMMDD_<framework>_<model>`, with normalized public harness names, for both scores and Hugging Face trajectories. Do not add model release dates or prices; the leaderboard repository resolves them after merge. Confirm that the `model-metadata` and `validate` checks pass.

## Tasks and failures

* Ground environments and failures in real network incidents, standards, vendor practice, or established failure modes.
* Model a failure as a root cause, not a symptom.
* Keep environments minimal while preserving realistic reproduction and diagnosis.
* Do not add telemetry, metadata, or interfaces that leak ground truth.
* Follow the `ProblemBase` model, failure taxonomy, registry, and RCA conventions in [Create benchmark tasks](docs/development/creating-benchmark-tasks.md).
* Put failure logic shared across domains in `src/nika/problems/support/`, not in a concrete registered failure.

## Benchmark contracts

Changes to environment/failure compatibility, taxonomy, root-cause identity, ground truth, registries, or benchmark tasks are benchmark contract changes. After such a change, regenerate the derived artifacts (`uv run nika benchmark generate`) and review the diff. Do not edit generated benchmark data by hand unless explicitly required. Published benchmark releases do not change.

## Testing

Executable network behavior needs real Docker-backed NIKA end-to-end runs; unit tests alone do not show that an incident, traffic pattern, or recovery works. For changes to environments, failures, traffic, telemetry, runtime, or backends:

* Start a real NIKA session and exercise the normal workflow.
* Use `nika exec` to inspect network state inside containers.
* Check observable behavior: connectivity, routes, protocol sessions, counters, traffic, telemetry, failure symptoms, and recovery.
* Prefer real protocol and runtime behavior to mocks or synthetic substitutes.

Run independent end-to-end tests in parallel when it is safe. The benchmark runner already runs heavy environments (Containerlab, k8s/llmd/XRd, and topology size `l`) one at a time through `heavy_batch_size: 1`; do not stack several heavy environments by hand in ad-hoc scripts.

Use unit tests for stable, isolated logic such as parsing, schemas, deterministic transformations, compatibility rules, and pure algorithms. [Testing](docs/development/testing.md) describes the suite layout, the required pytest tier markers, and the CI jobs.

Keep a small durable regression set that covers user-visible behavior, important failure modes, benchmark contracts, and reusable core logic. Do not add permanent tests that only mirror implementation details or raise coverage. Temporary tests and helpers are fine during development; remove them when the feature is done.

A test that creates external resources must clean up only what it owns, including on failure. Use NIKA lifecycle commands such as `nika session close` instead of manual Docker or emulator cleanup.

## Before you finish

* For executable network changes, verify the intended behavior in a running NIKA session.
* Confirm that the retained regression tests pass and that test-created resources are gone.
* Remove temporary or overly specific test code.
* Review the diff for unintended changes.
* Run `uv run ruff format` and `uv run ruff check`.

A test is not complete if its assertions pass but its resources remain, or if it does not exercise the intended runtime behavior.

## Operational safety

* Never delete unrelated runtime, Docker, emulator, Kubernetes, or experiment resources. Close your own sessions with `nika session close --session_id <id>`; reserve `nika session wipe` for when the user asks for it, because it closes every session.
* Preserve environment data, topology files, startup configs, P4 programs, manifests, and traffic datasets unless the task changes them.
