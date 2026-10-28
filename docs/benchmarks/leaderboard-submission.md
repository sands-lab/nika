# Leaderboard submission

This how-to is for benchmark operators who submit an official release run to:

- **Scores:** [`sands-lab/nika-leaderboard`](https://github.com/sands-lab/nika-leaderboard) (GitHub PR)
- **Trajectories:** [`nika-bench/nika-trajectories`](https://huggingface.co/datasets/nika-bench/nika-trajectories) (Hugging Face dataset PR)

Implementation: [`workflows/leaderboard/`](../../src/nika/workflows/leaderboard/) packs, validates, and submits both packages.

**Prerequisites:**

- A finished official `nika benchmark run --release …` against a current (non-deprecated) benchmark release, run from a checkout of its [release tag](#run-from-the-release-tag)
- Authenticated [`gh`](https://cli.github.com/) access (`gh auth login` or `GH_TOKEN` with `repo` scope)
- Hugging Face authentication through `hf auth login`, or `HF_TOKEN` in the environment / repo-root `.env`. Use a token that permits proposing dataset changes. You do not need collaborator access to the public dataset to open a PR. `uv sync` installs `huggingface_hub` and `hf`.

## Run from the release tag

Leaderboard validation computes the expected trials from the official task definitions: the `benchmark/releases/<version>/` files at the Git tag `release/<version>` in `sands-lab/nika`. Each trial ID includes a hash of its task definition. A run made from a checkout whose release files differ from the tag cannot pass validation, so check out the tag before you start the run:

```shell
git fetch --tags
git checkout release/0.2.0
uv run nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
```

When this checkout's release files differ from the tag, `nika benchmark run --release` lists the changed tasks and asks whether to continue before any trial runs. `submit` and `nika leaderboard validate` read the tagged files, so a package that passes locally also passes leaderboard CI.

## Submit with one command

After completing the official release run and logging in to both platforms:

```shell
uv run nika leaderboard submit --result_dir results/my-run \
  --name "My Agent" --authors "Your Name"
```

The command generates `metadata.yaml` and a short README using your name and authors, plus the model and agent framework recorded in the run. It packs and validates both packages, checks both accounts, then opens the GitHub scores PR and Hugging Face trajectories PR. Users without GitHub push access submit through a fork. With HTTPS, git uses the `gh` credentials; with SSH, you need a working GitHub SSH key. If you have no git author configured, the command uses your GitHub login for the submission commit.

To inspect packages before uploading, add `--dry-run` to the same command. This packs and validates locally, prints both destination paths, and requires no remote authentication.

The GitHub PR carries the `leaderboard-submit` label, which the leaderboard repository requires on submission PRs. Success prints both PR URLs. Maintainers still review and merge the two PRs before publishing the entry. The command does not merge PRs.

The one-command form creates minimal metadata. Before publishing, review the entry's code link, project links, and harness display name. Use custom metadata below when you need to supply these details.

For an open-weight model, `submit` also records which Hugging Face model the run served, so the leaderboard can find its release date and price. See [the open-weight model record](#record-the-open-weight-model).

## Submit with custom metadata

Use a template when you want to supply a longer README, code links, tools, skills, or other metadata. Pass `--submission` instead of `--name` and `--authors`.

```shell
# From a checkout of release/0.2.0.
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run -a <agent> -m <model>

nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md

nika leaderboard submit --result_dir results/my-run \
  --submission results/my-run/submission
# optional:
#   --dry-run
#   --hf-model org/name --hf-revision <commit|branch|tag>
#   --proprietary
#   --draft
#   --skip-github / --skip-trajectories
#   --traj-repo nika-bench/nika-trajectories
#   --title "..." --body "..."
#   --repo sands-lab/nika-leaderboard
#   --release-source local   # offline check against this checkout; CI still uses the tag
```

`submit` packs scores into `{result_dir}/{YYYYMMDD}_{slug}/` and trajectories into a sibling `{YYYYMMDD}_{slug}_trajectories/`, validates both (unless `--skip-validate`), then opens a GitHub PR under `submissions/<release_version>/{YYYYMMDD}_{slug}/` and a Hugging Face dataset PR under `trajectories/<release_version>/{YYYYMMDD}_{slug}/`. Pack or validate failures print an error and stop before any remote submit.

Authentication failures also stop before creating either PR. An upload can still fail after the GitHub PR opens, for example because of a network error or Hub upload permissions. The error prints the existing GitHub PR URL. Retry the same command with `--skip-github --out <original-scores-package-directory>` as printed in the error. This uploads trajectories under the original package name even when retrying on another date. The `--out` directory name must be `YYYYMMDD_<framework>_<model>`, using lowercase ASCII words separated by underscores. The framework is the public harness name: `cli.claude` becomes `claude_code`, and LangGraph becomes `react`.

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
- `validity_flags`: run-validity flags for the trial (empty when clean); see [run-validity gates](#run-validity-gates)

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

Remote path (same slug, no `_trajectories` suffix): `trajectories/<release_version>/{YYYYMMDD}_{slug}/`.

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
hf discussions list nika-bench/nika-trajectories --type dataset
hf discussions merge nika-bench/nika-trajectories <N> --yes --type dataset
```

## `metadata.yaml`

```yaml
info:
  name: ""            # required; display name
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

- `info.github`: link to the agent implementation or library, rather than the submitter's profile. For NIKA's built-in agents, use `https://github.com/sands-lab/nika`.
- `info.site` and `info.report`: supply the project site and relevant paper or report when available. Explain a benchmark-paper link in the README so readers can distinguish it from an agent report.
- `agent.framework`: use the public harness name, such as `Claude Code`, `Codex`, `OpenCode`, `DeepSeek Harness`, or `LangGraph`. The run identity retains the NIKA agent ID, such as `cli.claude`.
- `agent.tools` and `agent.skills`: describe what the agent used. Copy enabled MCP server names from the run's `tools.allowed_mcp_servers` when appropriate.
- `agent.extra`: record the official model repository and the evaluated revision when available, along with serving precision or other configuration needed to reproduce the run. Do not guess a revision after the run.
- `agent.optimization_methods`: free-form strings describing **harness optimizations** applied around the model (prompting, skills, multi-agent orchestration, GEPA-style search, etc.). Prefer short labels such as `GEPA`, `skills`, or `Multi-agent`. This is not limited to weight fine-tuning.

## Model release dates, prices, and display names

You do not supply release dates or prices. After your scores PR merges, the [leaderboard repository](https://github.com/sands-lab/nika-leaderboard) looks up the model's release date and reference price from public catalogs and opens a catalog PR for its maintainers to review. Until that PR merges, the website shows the entry's cost as `pending`; models with no public quote show `n/a`. See the leaderboard README for the catalog format and sources.

### Record the open-weight model

Open-weight prices and release dates come from the model's Hugging Face repo, so `submit` writes the repo and a pinned commit to `agent.extra`:

```yaml
agent:
  extra:
    hf_repo: nvidia/Qwen3.6-35B-A3B-NVFP4
    hf_revision: 1355db6a052410cfd62085d94b58866fd0f2c3c5
    hf_revision_source: latest   # or: specified
```

When a custom endpoint served the model under its Hugging Face ID, as vLLM does by default, `submit` finds the repo from the run's `model` and needs no extra flags. Unless you pass `--hf-revision`, it records the repo's latest revision at submit time and prints it. Pass `--hf-revision` when the run served an older commit, branch, or tag; `submit` stores its commit SHA.

`submit` stops before packing when it cannot identify the model, for example when vLLM used `--served-model-name` or Ollama served a library model such as `gpt-oss:120b`. Pass the repo explicitly, or mark a proprietary model behind a custom endpoint:

```shell
uv run nika leaderboard submit --result_dir results/my-run \
  --name "My Agent" --authors "Your Name" \
  --hf-model openai/gpt-oss-120b      # or: --proprietary
```

Runs against a vendor API (`openai`, `anthropic`, `deepseek`) record nothing.

### How the website prices runs

The leaderboard uses vendor API list prices for proprietary models and public hosted-inference quotes for open weights. For open weights, this is a reference estimate for hosted inference, not the submitter's self-hosted GPU bill. Each run uses the quote that was current on its package date; a **Current prices** toggle re-prices every run at the latest quote.

The website computes the token-priced total as `(input_tokens × input_rate + output_tokens × output_rate) / 1,000,000`. Cost per run means one attempt at every case: `total_cost / reported_trials × case_count`. Token counts come from the packed run. The reference calculation does not apply cache discounts or add network emulation and GPU allocation costs.

Chart names use the model, normalized harness name, optimization methods, and skills. An optimization's `agent.extra.<method>_variant` field adds its variant, such as `gepa_variant: d+s`. Repeated configurations include the split and run identity to distinguish them. The submission detail page retains your original `info.name`. Packages use `YYYYMMDD_<framework>_<model>`; for example, `20261012_claude_code_gemma_4_31b_it`.

## `README.md`

Short system description, authors, and links to code / report / site (if any).

## What submit validates

- Identity and trial IDs match the official release at its [release tag](#run-from-the-release-tag), and the task definitions at the run's recorded NIKA commit match it too
- Exact trial coverage (`case_count × n_trials`); metrics and `rca_confusion.json` match recomputed aggregates (non-`scored` trials count as 0 in primary means)
- No `grading_error` or residual `infra_error` scoring status (`infra_error` is auto-retried during the run; leftovers reject the pack)
- Run-validity flags (below): `nika leaderboard validate` warns on any flag and fails when `truncated_final_turn` or the `infra_error` validity flag exceeds `--max-harness-rate` of trials (default 5%)
- Sibling trajectories package has the required per-trial files and matching trial set
- No secrets or absolute paths in package text

See also [root-cause ground truth and scoring](root-cause-evaluation.md#score-a-submission) for per-trial metrics, `score_status`, and aggregation.

### Run-validity gates

`nika.evaluator.validity` sets flags from artifacts in each trial directory (`messages.jsonl`, `submission.json`, `nika.jsonl`, `run.json`). Readers recompute them from those artifacts for older runs. `nika eval summary` prints per-flag trial counts and rates (`validity_counts` / `validity_rates` in the JSON report). `nika leaderboard pack` stores `validity_flags` in each trial `result.json` and `validity_counts` in `metrics.json`.

| Flag | Meaning |
| --- | --- |
| `no_submission` | no readable `submission.json` with a `root_causes` list |
| `empty_report` | frozen diagnosis report is empty or only opening narration before the first tool call |
| `truncated_final_turn` | the last LLM turn of a phase hit the output limit (`finish_reason: length`, `stop_reason: max_tokens`, or output tokens equal `max_tokens` with no text) |
| `max_steps_exhausted` | diagnosis hit `max_steps`; details record whether a wrap-up report exists |
| `fault_absent_at_cleanup` | a `fault_presence_recheck` before cleanup read `absent`: the agent likely mutated the network or the fault self-healed |
| `infra_error` | the trial failed to start, lacks ground truth, or has `score_status=infra_error` |

Pack rejection on `score_status` has no threshold: any trial with `grading_error` or residual `infra_error` status rejects the package. A trial can carry the `infra_error` validity flag without that scoring status. The remaining flags only warn.

## Validation errors

Each finding has a stable code, a cause, and a fix. To check a packed scores package as leaderboard CI does:

```shell
uv run nika leaderboard validate results/my-run/20261027_codex_gpt_6_astra
```

`--format json` prints a machine-readable report and `--format markdown` prints a pull request comment section; both include the release commit the package was checked against. The command exits with status 1 when the package has errors. `--release-source local` checks against this checkout's release files when GitHub cannot be reached; leaderboard CI does not accept that result.

### `ERROR_RELEASE_DRIFT`

The package was produced with task definitions that differ from the official release, usually because the NIKA checkout used for the run is older or newer than the release tag. NIKA reports this when a task's trials are all missing but trials with the same task name and a different hash are present, or when the release files at the run's recorded NIKA commit define a task differently, for example with other ground truth. The error lists the changed tasks and the official commit. Check out the release tag, re-run those tasks into the same result directory with `nika benchmark run --release <version> --split <split> --result_dir <dir> --no-resume --task-id <task> …`, and pack again.

### `ERROR_TRIALS_MISSING`

Some case × trial slots of the release have no result. Resume the run until every trial finishes, then pack again.

### `ERROR_TRIALS_UNEXPECTED`

The package contains trials that the release does not define, for example from another split or an ad-hoc run. Re-pack from the release run directory.

### `ERROR_RELEASE_UNAVAILABLE`

NIKA could not read the official release: the version has no release tag yet, or GitHub could not be reached. Check the version and the network connection.

### `ERROR_BENCHMARK_MISMATCH`

The package identity (benchmark ID, version, split, case count, trials per case, or scoring) differs from the release. Run the release itself with `nika benchmark run --release <version>` and pack that run.

### `ERROR_SCORE_MISMATCH`

`metrics.json` or `rca_confusion.json` differs from the aggregate NIKA recomputes from the per-trial results. Re-pack instead of editing package files.

### `ERROR_GRADING_ERROR`

Some trials have `score_status=grading_error`. Repair the grading inputs, re-run those trials, and pack again.

### `ERROR_INFRA_ERROR`

Some trials still have `score_status=infra_error` after the run's retries. Resume the run until none remain.

### `ERROR_RUN_VALIDITY`

`truncated_final_turn` or the `infra_error` validity flag exceeds `--max-harness-rate` of trials; see [run-validity gates](#run-validity-gates). Fix the harness configuration, such as the output token limit, and re-run the affected trials.

### `ERROR_UNOFFICIAL_RUN`

The run was not started with `nika benchmark run --release`, so `run.official` is false.

### `ERROR_SECRET_FOUND`

Package text contains a possible credential or an absolute host path. Remove the value from `metadata.yaml`, `README.md`, or the run artifacts it came from, and pack again.

### `ERROR_PACKAGE_INCOMPLETE`

A required package file or directory is missing. Re-pack with `nika leaderboard submit --dry-run`.

### `ERROR_METADATA_INVALID`

`metadata.yaml` misses a required field, has an unknown field, or does not parse. Compare it with `nika leaderboard template`.

### `ERROR_IDENTITY_INVALID`

`identity.yaml` does not match the package schema, usually because it was edited by hand or written by a NIKA version with another schema. Re-pack.

### `ERROR_RESULTS_INVALID`

A metrics, RCA confusion, or per-trial result file does not match the package schema, or a trial directory name differs from its trial ID. Re-pack.

### `ERROR_TRAJECTORY_INVALID`

A trajectory trial lacks a [required file](#trajectories-package-layout-hugging-face), or a JSON Lines file does not parse. Re-pack.

### `ERROR_TRAJECTORY_FORBIDDEN_FILE`

The trajectories package contains packet captures, validation dumps, or judge artifacts, which are not published. Re-pack.

### `ERROR_PACKAGE_PAIR_MISMATCH`

The scores and trajectories packages differ in benchmark identity, run, or trial set. Re-pack both in one `nika leaderboard submit` call.

### `WARNING_LOCAL_RELEASE`

The package was checked against this checkout's release files (`--release-source local`). Validate without that option before submitting.

### `WARNING_RUN_COMMIT_UNVERIFIED`

The NIKA commit recorded in the package is neither in the local repository nor on GitHub, so NIKA compared trial IDs only. Run from the release tag, or push the commit the run used.

### `WARNING_RUN_VALIDITY`

Some trials carry run-validity flags that do not block a submission, such as `no_submission`. Review the flagged trials.

### `WARNING_NON_DEFAULT_PRIMARY`

The release declares a primary metric other than `rca_f1`. No action is needed when the release defines it.

## Check the pull requests

### GitHub (scores)

- [ ] Official release run; local pack/validate during `nika leaderboard submit` passed
- [ ] Required metadata fields filled; README describes the system
- [ ] Code, project/report links, public harness name, and tools reviewed against the run
- [ ] Open-weight model: `agent.extra.hf_repo` and `hf_revision` match the served model
- [ ] Required `model-metadata` check passes (a new model is reported as `pending`)
- [ ] Path is `submissions/<version>/{YYYYMMDD}_{slug}/`
- [ ] CI validate workflow is green

### Hugging Face (trajectories)

- [ ] Path is `trajectories/<version>/{YYYYMMDD}_{slug}/`
- [ ] Paired scores package opened / linked on GitHub
- [ ] Trial set matches the release (`case_count × n_trials`)
- [ ] No secrets or absolute machine paths in package text
- [ ] Maintainer merges the Hub Discussion/PR after review
