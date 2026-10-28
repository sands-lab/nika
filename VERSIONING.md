# Versioning policy

NIKA uses one version number for the benchmark task dataset and the orchestrator, so results stay comparable while the software keeps evolving. Here, *task dataset* means the benchmark's task definitions and their evaluation data, not the agent trajectories collected from runs.

The version has the form **`EDITION.REVISION.PATCH`**. The first two components identify the benchmark revision used for evaluation; the third identifies software updates within that revision.

| Component | Meaning | Increment it for | Example |
| --- | --- | --- | --- |
| **Edition** | A new benchmark edition | A substantial expansion of the task set, or a significant redesign of the benchmark or its evaluation protocol. | `1.x.y` → `2.0.0` |
| **Revision** | A correction to the current edition | Corrections to existing tasks, ground truth, scoring, or execution behavior, such as failure injection, that has repoertedly affectect evaluation and required one or more fixes. | `1.0.x` → `1.1.0` |
| **Patch** | An orchestrator update that preserves the benchmark | Software fixes or improvements that do not materially change the tasks, evaluation conditions, or scoring. | `1.0.0` → `1.0.1` |

When a component increases, the components to its right reset to zero.

Classify a change by its effect, not by where it is implemented. A change made entirely in the orchestrator still needs a new revision if it changes what the agent encounters or how its answer is evaluated.

## Releases and reproducibility

Published releases do not change. Each one is identified by a Git tag, and a correction produces a new version. To run an earlier release, check out its tag; the latest branch does not have to run every historical benchmark.

Patches can be frequent. Group benchmark corrections into infrequent revisions where practical, and publish new editions as deliberate releases.

| Where | Shows | Example |
| --- | --- | --- |
| Leaderboard and UI | Benchmark revision | `1.0` |
| GitHub releases | Full version | `1.0.3` |
| Run provenance | Benchmark revision, full version, and Git commit | `1.0`, `1.0.3`, commit SHA |

Results from different benchmark revisions must stay distinguishable, because a correction can change scores even when the task set is unchanged.
