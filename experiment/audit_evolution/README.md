# Audit knowledge evolution

Compare audits that accumulate knowledge with audits that start fresh. Each step reads one recorded evaluation run; it does not deploy that run's network. The experiment records findings, verification, knowledge-base changes and cumulative coverage of a separate ground-truth set.

## Prepare the inputs

Run from the repository root. NIKA's Python dependencies, Claude Code and `bubblewrap` must be available. Model calls use the KAUST gateway or a configured OpenAI model. Configure credentials through the paths and environment variables in `common.py`; keep credentials outside this repository.

Set `AUDIT_EVO_ROOT` to the experiment artifact directory, and `AUDIT_EVO_MODEL` to a distinct output namespace for each auditor model:

```sh
export AUDIT_EVO_ROOT=/workspace/nika-audit-evolution
export AUDIT_EVO_MODEL=auditor-model
```

Prepare and pin these inputs before starting replay:

| Location under `AUDIT_EVO_ROOT` | Input |
|---|---|
| `data/<benchmark>/<run>/trials/` | Recorded trial packages |
| `sandbox/tree/` | Prepared historical NIKA source with its audit library, skill, empty knowledge base and `.venv` |
| `sandbox/repo/` | Git objects for the recorded source commits |
| `gt/` | Ground truth and any signature-match reports |

The sandbox exposes only selected input and work directories to the auditing agent. Ground truth and the production knowledge base remain outside that view. These scripts expect a prepared sandbox; they do not download the inputs or construct the entire sandbox automatically.

`gt.py` also uses the fixed `/workspace/nika` checkout and its production knowledge base. Review those bindings before running on another host. The scorer's model and credential settings are defined in `score.py`.

## Replay an experiment arm

```sh
uv run python -m experiment.audit_evolution.replay --help
uv run python -m experiment.audit_evolution.replay \
  --bench nika-0.2.0 --arm ours --alias MODEL_ALIAS --seed 0
```

| Arm | Information retained between evaluation runs |
|---|---|
| `ours` | Guidelines and a growing knowledge base of records, signatures and lessons |
| `b1` | Guidelines; empty memory for each run |
| `b2` | Guidelines and raw findings from earlier runs |
| `b3` | Open-ended agent audit with the ABC checklist and no historical memory |

`ours` and `b2` process runs sequentially. `b1` and `b3` can use `--jobs` for independent steps. Seed zero preserves sorted run order; other seeds shuffle it. Use `--limit` for a pilot and set worker and token budgets to match the comparison you intend to report.

Step directories live under `runs/<model>/<benchmark>/<arm>/s<seed>/`. `done.json` and `failed.json` distinguish completed and failed steps. Inspect failures before interpreting cumulative detection counts.

## Score and inspect

```sh
uv run python -m experiment.audit_evolution.score \
  --bench nika-0.2.0 --arm ours --seed 0
```

Scoring reads the corresponding ground truth and completed steps. It writes coverage results and unmatched findings under `results/<model>/<benchmark>/` and caches model judgments under the model-specific cache directory. Scoring makes model calls; it is not a purely deterministic recount.

| Module | Purpose |
|---|---|
| `gt.py` | Build the production-issue reference set from signatures, scopes and run-level records |
| `plant.py` | Build a separate dataset with planted defects and verify that originals remain unchanged |
| `scan_all.py` | Precompute repeated scans for a prepared benchmark dataset |
| `stage_review.py` | Run scan, adjudication, verification and knowledge recording for one step |
| `netopsbench.py` | Convert external trajectories and construct their reference set |
| `spot_keeper.py` | Manage the experiment's gateway spot deployments |

Use each module's `--help` where provided. `plant.py` rebuilds its configured destination, so use a dedicated artifact root. `spot_keeper.py` changes gateway deployments and is an operational helper, not part of replay or scoring. The historical `0.2.0` inputs and preparation bindings require review before this workflow can support a new dataset.
