# Leaderboard submission

This how-to is for benchmark operators who submit an official release run to:

- **Scores:** [`sands-lab/nika-leaderboard`](https://github.com/sands-lab/nika-leaderboard) (GitHub PR)
- **Trajectories:** [`Zhihao98/nika-trajectories`](https://huggingface.co/datasets/Zhihao98/nika-trajectories) (Hugging Face dataset PR)

Implementation: [`workflows/leaderboard/`](../../src/nika/workflows/leaderboard/) packs, validates, and submits both packages.

**Prerequisites:**

- A finished official `nika benchmark run --release …` against a current (non-deprecated) frozen release
- Authenticated [`gh`](https://cli.github.com/) access (`gh auth login` or `GH_TOKEN` with `repo` scope)
- Hugging Face authentication through `hf auth login`, or `HF_TOKEN` in the environment / repo-root `.env`. Use a token that permits proposing dataset changes. You do not need collaborator access to the public dataset to open a PR. `uv sync` installs `huggingface_hub` and `hf`.

## Submit with one command

After completing the official release run and logging in to both platforms:

```shell
uv run nika leaderboard submit --result_dir results/my-run \
  --name "My Agent" --authors "Your Name"
```

The command generates `metadata.yaml` and a short README using your name and authors, plus the model and agent framework recorded in the run. It packs and validates both packages, checks both accounts, then opens the GitHub scores PR and Hugging Face trajectories PR. Users without GitHub push access submit through a fork. With HTTPS, git uses the `gh` credentials; with SSH, you need a working GitHub SSH key. If you have no git author configured, the command uses your GitHub login for the submission commit.

To inspect packages before uploading, add `--dry-run` to the same command. This packs and validates locally, prints both destination paths, and requires no remote authentication.

Success prints both PR URLs. Maintainers still review and merge the two PRs before publishing the entry. The command does not merge PRs.

## Submit with custom metadata

Use a template when you want to supply a longer README, code links, tools, skills, or other metadata. Pass `--submission` instead of `--name` and `--authors`.

```shell
# Use a current frozen release when published. 0.1.0 is deprecated; use 0.2.0.
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run -a <agent> -m <model>

nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md

nika leaderboard submit --result_dir results/my-run \
  --submission results/my-run/submission
# optional:
#   --dry-run
#   --draft
#   --skip-github / --skip-trajectories
#   --traj-repo Zhihao98/nika-trajectories
#   --title "..." --body "..."
#   --repo sands-lab/nika-leaderboard
```

`submit` packs scores into `{result_dir}/{YYYYMMDD}_{slug}/` and trajectories into a sibling `{YYYYMMDD}_{slug}_trajectories/`, validates both (unless `--skip-validate`), then opens a GitHub PR under `submissions/<release_version>/{YYYYMMDD}_{slug}/` and a Hugging Face dataset PR under `trajectories/<release_version>/{YYYYMMDD}_{slug}/`. Pack or validate failures print an error and stop before any remote submit.

Authentication failures also stop before creating either PR. An upload can still fail after the GitHub PR opens, for example because of a network error or Hub upload permissions. The error prints the existing GitHub PR URL. Retry the same command with `--skip-github --out <original-scores-package-directory>` as printed in the error. This uploads trajectories under the original package name even when retrying on another date. The `--out` directory name must be `YYYYMMDD_<slug>` with the slug derived from the entry name.

`--skip-validate` skips local checks only. GitHub CI still validates scores. A run containing only selected tasks or a single trial per case cannot satisfy the full release coverage unless the result directory already contains all required trials. For release 0.2.0, the test split requires 85 cases × 3 trials = 255 finished slots; the dev split requires 84 × 3 = 252. Agent failures and timeouts can count as finished slots with zero primary score. Infrastructure or grading errors need repair and rerun before submission.

Agents submit `(resource_id, fault_type)` pairs. Scoring uses set metrics on those pairs.

## Scores package layout (GitHub)

```text
{YYYYMMDD}_{slug}/
  README.md
  metadata.yaml
  results/
    identity.yaml          # includes trajectories_relpath
    metrics.json
    rca_confusion.json     # multi-label GT→predicted edge counts
    trials/{trial_id}/result.json
```

Remote path: `submissions/<release_version>/{YYYYMMDD}_{slug}/`. Traces are not stored on GitHub; identity binds the named release version + split and points at the HF trajectories path.

### Per-trial `result.json`

In addition to metrics, each trial records fault-type labels for confusion-matrix display:

- `gt_fault_types`: unique `fault_type` values from session `ground_truth.json` `root_causes` (fallback: `[problem]`)
- `predicted_fault_types`: unique `fault_type` values from session `submission.json` `root_causes`, or `null` when the file is missing or has no pairs

`results/rca_confusion.json` aggregates multi-label edges `(gt, predicted)` across trials and records `n_missing_prediction` / `missing_prediction_trial_ids`.

## Trajectories package layout (Hugging Face)

```text
{YYYYMMDD}_{slug}_trajectories/          # local sibling written by submit
  README.md
  metadata.yaml
  identity.yaml                          # scores_package + trajectories_relpath
  trials/{trial_id}/
    run.json
    messages.jsonl
    nika.jsonl
    ground_truth.json
    eval_metrics.json
    submission.json                      # required when outcome=success
```

Remote path (same slug, no `_trajectories` suffix):
`trajectories/<release_version>/{YYYYMMDD}_{slug}/`.

`submit` packs only these per-trial files from each session directory:

| File | Role |
|------|------|
| `run.json` | Session meta, task text, outcome |
| `messages.jsonl` | Agent diagnosis / submission transcript |
| `nika.jsonl` | NIKA system / workflow events |
| `ground_truth.json` | Expected RCA labels |
| `eval_metrics.json` | Rule-based scores + token/step counters |
| `submission.json` | Agent final RCA answer (required when `outcome=success`) |

After review, maintainers merge the Hub Discussion/PR. Optional Hub CLI:

```shell
hf discussions list Zhihao98/nika-trajectories --type dataset
hf discussions merge Zhihao98/nika-trajectories <N> --yes --type dataset
```

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
  extra: {}
```

Field notes:

- `agent.optimization_methods`: free-form strings describing **harness optimizations** applied around the model (prompting, skills, multi-agent orchestration, GEPA-style search, etc.). Prefer short labels such as `GEPA`, `skills`, or `Multi-agent`. This is not limited to weight fine-tuning.

## `README.md`

Short system description, authors, and links to code / report / site (if any).

## What submit validates

- Identity matches the in-tree frozen release
- Exact trial coverage (`case_count × n_trials`); metrics and `rca_confusion.json` match recomputed aggregates (non-`scored` trials count as 0 in primary means)
- No `grading_error` or residual `infra_error` trials (`infra_error` is auto-retried during the run; leftovers reject the pack)
- Sibling trajectories package has the required per-trial files and matching trial set
- No secrets or absolute paths in package text

See also [root-cause ground truth and scoring](root-cause-evaluation.md#score-a-submission) for per-trial metrics, `score_status`, and aggregation.

## Check the pull requests

### GitHub (scores)

- [ ] Official release run; local pack/validate during `nika leaderboard submit` passed
- [ ] Required metadata fields filled; README describes the system
- [ ] Path is `submissions/<version>/{YYYYMMDD}_{slug}/`
- [ ] CI validate workflow is green

### Hugging Face (trajectories)

- [ ] Path is `trajectories/<version>/{YYYYMMDD}_{slug}/`
- [ ] Paired scores package opened / linked on GitHub
- [ ] Trial set matches the release (`case_count × n_trials`)
- [ ] No secrets or absolute machine paths in package text
- [ ] Maintainer merges the Hub Discussion/PR after review
