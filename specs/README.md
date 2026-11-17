# NIKA specifications

These pages state the contracts and internals that the code implements: interfaces, schemas, registries, verification rules, and data catalogs. Developers and coding agents read them when they change the code. Human-facing guides live in [`docs/`](../docs/README.md), which links here instead of repeating these contracts.

When code changes a behavior described here, the same pull request updates the page. A reviewer checks this with the specs review workflow.

| Page | Describes |
| --- | --- |
| [Benchmark release contracts](benchmark-contracts.md) | The frontend and benchmark engine interface, release manifests, and lifecycle protocol |
| [Scenario validation](scenario-validation.md) | Healthy-baseline verifiers, the scenario registry, and Batfish static checks |
| [Scenario construction and verification](scenario-verification.md) | Shared design requirements and the offline experiments that build and verify environments |
| [Leaderboard submission internals](leaderboard-internals.md) | Release checks, result packaging, and score validation for `nika leaderboard` |
| [Failure sources](failure-sources.json) | Public evidence catalog for each failure class, exported by `scripts/failure_sources.py` |
