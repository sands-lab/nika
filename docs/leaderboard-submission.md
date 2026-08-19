# Leaderboard submission

This how-to is for benchmark operators who submit an official release run to [`sands-lab/nika-leaderboard`](https://github.com/sands-lab/nika-leaderboard).

Implementation: [`workflows/leaderboard/`](../src/nika/workflows/leaderboard/) builds, validates, and submits packages.

**Prerequisites:** a finished `nika benchmark run --release …` and authenticated [`gh`](https://cli.github.com/) access (`gh auth login` or `GH_TOKEN` with `repo` scope).

## Submit a release run

```shell
nika benchmark run --release 0.1.0 --result_dir results/my-run -a <agent> -m <model>

nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md

nika leaderboard pack --result_dir results/my-run \
  --submission results/my-run/submission

nika leaderboard validate results/my-run/YYYYMMDD_slug \
  --source-result-dir results/my-run

nika leaderboard submit results/my-run/YYYYMMDD_slug
# optional: --draft --title "..." --body "..." --repo sands-lab/nika-leaderboard
```

`pack` writes `{result_dir}/{YYYYMMDD}_{slug}/` (slug from `metadata.info.name`; override with `--out`). `submit` validates (unless `--skip-validate`), pushes to `submissions/<release_version>/{YYYYMMDD}_{slug}/`, and opens a PR. CI re-validates packages under `submissions/`.

Agents submit `(resource_id, fault_type)` pairs. Scoring uses set metrics on those pairs.

## Package layout

```text
{YYYYMMDD}_{slug}/
  README.md
  metadata.yaml
  files.json
  results/
    identity.yaml
    metrics.json
    rca_confusion.json     # multi-label GT→predicted edge counts
    trials/{trial_id}/result.json
```

Remote path: `submissions/<release_version>/{YYYYMMDD}_{slug}/`. Traces and per-case run artifacts are not included; integrity uses `source_run_sha256` and the frozen `benchmark.digest`.

### Per-trial `result.json`

In addition to metrics, each trial records fault-type labels for confusion-matrix display:

- `gt_fault_types`: unique `fault_type` values from session `ground_truth.json` `root_causes` (fallback: `[problem]`)
- `predicted_fault_types`: unique `fault_type` values from session `submission.json` `root_causes`, or `null` when the file is missing or has no pairs

`results/rca_confusion.json` aggregates multi-label edges `(gt, predicted)` across trials and records `n_missing_prediction` / `missing_prediction_trial_ids`.

## `metadata.yaml`

```yaml
info:
  name: ""            # required; display name + folder slug
  authors: ""         # required
  org: null
  site: null
  report: null
  logo: null
  email: null
  github: null
agent:
  model: ""           # required
  framework: ""       # required
  tools: []
  skills: []
  # Harness optimizations (not model fine-tunes). Examples: GEPA, skills, Multi-agent
  optimization_methods: []
  tags: []
  os_model: false
  os_system: false
  extra: {}
```

Field notes:

- `agent.optimization_methods`: free-form strings describing **harness optimizations** applied around the model (prompting, skills, multi-agent orchestration, GEPA-style search, etc.). Prefer short labels such as `GEPA`, `skills`, or `Multi-agent`. This is not limited to weight fine-tuning.
- `agent.os_model` / `agent.os_system`: optional openness flags kept for package metadata; the public leaderboard UI filters primarily by model name and harness optimizations.

## `README.md`

Short system description, authors, and links to code / report / site (if any).

## Validate a package

- Identity matches the in-tree frozen release
- Exact trial coverage (`case_count × n_trials`); metrics and `rca_confusion.json` match recomputed aggregates (failures count as 0 in means)
- Package hashes in `files.json`; with `--source-result-dir`, `run.json` matches `source_run_sha256`
- No secrets or absolute paths in package text

## Check the pull request

- [ ] Official release run; local `nika leaderboard validate` passed
- [ ] Required metadata fields filled; README describes the system
- [ ] Path is `submissions/<release_version>/{YYYYMMDD}_{slug}/`
- [ ] CI validate workflow is green
