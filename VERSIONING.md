# Versioning policy

NIKA uses one version number for the benchmark task dataset and the orchestrator, so results stay comparable while the software keeps evolving. Here, *task dataset* means the benchmark's task definitions and their evaluation data, not the agent trajectories collected from runs.

The version has the form **`EDITION.REVISION.PATCH`**. The first two components identify the benchmark revision used for evaluation; the third identifies software updates within that revision.

| Component | Meaning | Increment it for | Example |
| --- | --- | --- | --- |
| **Edition** | A new benchmark edition | A substantial expansion of the task set, or a significant redesign of the benchmark or its evaluation protocol. | `1.x.y` → `2.0.0` |
| **Revision** | A correction to the current edition | Corrections to existing tasks, ground truth, scoring, or execution behavior, such as failure injection, that affect evaluation. | `1.0.x` → `1.1.0` |
| **Patch** | An orchestrator update that preserves the benchmark | Software fixes or improvements that do not materially change the tasks, evaluation conditions, or scoring. | `1.0.0` → `1.0.1` |

When a component increases, the components to its right reset to zero.

Classify a change by its effect, not by where it is implemented. A change made entirely in the orchestrator still needs a new revision if it changes what the agent encounters or how its answer is evaluated.

## Releases and reproducibility

Published releases do not change. Release `0.3.0` is the first tagged release with a benchmark engine, built from `v0.3.0`. Corrections to tasks, ground truth, scoring, or agent-visible behavior require a new benchmark revision.

Reproduce releases `0.1.0` and `0.2.0` only from their published code:

```bash
git checkout ac4422d8a4a03be2b905e28edf72993d467e1b81
./scripts/install.sh
```

Run the release from that checkout after installation. These releases have no benchmark engine and cannot run on current NIKA. Leaderboard validation reads their original task files at that commit and preserves their fault IDs. Historical packages with different definitions report release drift; their scores remain unchanged.

Patches can be frequent. Group benchmark corrections into infrequent revisions where practical, and publish new editions as deliberate releases.

| Where | Shows | Example |
| --- | --- | --- |
| Leaderboard and UI | Benchmark revision | `1.0` |
| GitHub releases | Full version | `1.0.3` |
| Run provenance | Benchmark revision, full version, and Git commit | `1.0`, `1.0.3`, commit SHA |

Results from different benchmark revisions must stay distinguishable, because a correction can change scores even when the task set is unchanged.
