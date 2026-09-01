# NIKA

NIKA is a platform for generating and running executable benchmarks for network troubleshooting agents. Python dependencies and commands use `uv`.

## Development rules

* Read relevant code before modifying behavior and make the smallest change needed.
* Reuse existing NIKA abstractions instead of creating parallel implementations.
* Preserve benchmark reproducibility, ground truth, telemetry semantics, and backend neutrality.
* Do not weaken tests to make changes pass.
* Avoid introducing feature-specific helpers, wrappers, mocks, or abstractions unless they have clear reuse value.

## Scenarios and failures

* Prefer failures and scenarios grounded in real network incidents, standards, vendor practice, or established failure modes.
* Failures should represent root causes rather than symptoms.
* Keep scenarios minimal while preserving realistic reproduction and diagnosis.
* Avoid telemetry, metadata, or interfaces that leak ground truth.
* New failures follow the existing `ProblemBase` model, failure taxonomy, registry, and RCA conventions.
* Cross-domain reusable failure logic belongs in shared support code rather than concrete registered failures.

## Benchmark contracts

Treat changes to scenario/failure compatibility, taxonomy, root-cause identity, ground truth, registries, or benchmark cases as benchmark contract changes.

Regenerate and review derived benchmark artifacts after such changes. Do not manually modify generated benchmark data unless explicitly required. Treat published benchmark releases as immutable.

## Testing

Prefer **real Docker-backed NIKA E2E tests** over unit tests for executable network behavior.

For changes involving scenarios, failures, traffic, telemetry, runtime, or backends:

* Start a real NIKA session and exercise the normal workflow.
* Use `nika exec` to run commands inside containers and inspect actual network state.
* Validate observable behavior such as connectivity, routes, protocol sessions, counters, traffic, telemetry, failure symptoms, and recovery.
* Prefer real protocol and runtime behavior over mocks or synthetic substitutes.

Run independent E2E tests in parallel when safe to reduce test time, but run resource-intensive Kubernetes, LLMd, and containerlab scenarios sequentially to avoid resource contention and instability.

Use unit tests mainly for stable isolated logic such as parsing, schemas, deterministic transformations, compatibility rules, and pure algorithms.

Do not add permanent unit tests merely to mirror implementation details or increase coverage.

During feature development, temporary focused tests and helpers are acceptable. After completing each feature, proactively remove all development-only test functions, fixtures, mocks, scripts, and helper functions introduced for that work. Retain only durable tests and reusable helpers that directly exercise or support NIKA’s primary workflows, user-visible behavior, benchmark contracts, or reusable core logic.

Keep a small durable regression set focused on:

* user-visible behavior;
* important failure modes;
* benchmark contracts;
* reusable core logic.

Any test that creates external resources must clean up only the resources it owns, including on failure. Prefer NIKA lifecycle commands/APIs over manual Docker or emulator cleanup.

## Verification

For executable network changes, completion normally requires a real E2E run rather than only unit tests.

Before finishing:

* verify the intended behavior in a running NIKA environment;
* inspect containers with `nika exec` when relevant;
* confirm retained regression tests pass;
* confirm test-created resources are removed;
* remove temporary or overly specific test code;
* review the repository diff for unintended changes;
* run formatting and lint checks.

A test is not complete if assertions pass but its resources remain or it does not validate the intended runtime behavior.

## Operational safety

* Never delete unrelated runtime, Docker, emulator, Kubernetes, or experiment resources.
* Prefer NIKA lifecycle operations for session cleanup.
* Preserve scenario data, topology files, startup configs, P4 programs, manifests, and traffic datasets unless the task explicitly changes them.
