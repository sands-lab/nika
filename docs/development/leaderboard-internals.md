# Leaderboard submission internals

Maintainer and coding-agent reference for `nika leaderboard` (template, pack, validate, submit). The user guide is [Leaderboard submission](../benchmarks/leaderboard-submission.md); it deliberately hides everything below. When you change the workflow, update this file and keep the user guide limited to user-visible behavior (see the [NIKA documentation skill](../../.agents/skills/nika-docs/SKILL.md)).

## Module map

All code lives in [`src/nika/workflows/leaderboard/`](../../src/nika/workflows/leaderboard/).

| Module | Responsibility |
| --- | --- |
| `submit.py` | Orchestrates pack → validate → account checks → GitHub PR → Hugging Face PR. Stops before any remote step on pack, validation, or authentication failure. |
| `pack.py` | Builds the scores package and the sibling trajectories package from a finished release-run directory. |
| `validate.py` | Local validation of a scores package (identity, coverage, aggregates, score status, run validity, secrets). Same checks as leaderboard CI. |
| `validate_trajectories.py` | Validates the trajectories package: required files, forbidden files, JSONL parsing, trial-set pairing. |
| `release_check.py` | Compares package or result-dir trial IDs against the official release; detects release drift. |
| `issues.py` | Stable finding codes (`ERROR_*`, `WARNING_*`) with cause and fix text. Severity is derived from the prefix. |
| `trial_results.py` | Builds `TrialResult` records from session directories. Shared by leaderboard packing and offline eval reporting so both derive outcome, fault types, and metrics identically. |
| `aggregate.py` | Aggregates per-trial metrics into `metrics.json` and `rca_confusion.json`. |
| `meta_input.py` | `metadata.yaml` / `README.md` templates, loaders, slug derivation (`submission_slug`, `slugify_name`, public harness map). |
| `hf_model.py` | Resolves and pins the open-weight Hugging Face repo and revision into `agent.extra`. |
| `schema.py` | Package schemas, `ScoreStatus`, `TRAJECTORY_REQUIRED_FILES`, `TRAJECTORY_OPTIONAL_SUCCESS_FILE`, `TRAJECTORY_FORBIDDEN_NAME_FRAGMENTS`. |
| `secrets.py` | Scans package text for credentials and absolute host paths. |
| `github_cli.py`, `hf_cli.py` | Thin `gh`/`git` and Hugging Face Hub wrappers (fork handling, PR creation). |
| `remote.py`, `hf_remote.py` | Default repo constants (`sands-lab/nika-leaderboard`, `nika-bench/nika-trajectories`) and remote path helpers. |

Run-validity flags live in [`src/nika/evaluator/validity.py`](../../src/nika/evaluator/validity.py); score status in [`src/nika/evaluator/score_status.py`](../../src/nika/evaluator/score_status.py).

## Invariants that couple code and docs

- `issues.py` links each finding to the user guide section whose anchor is the lower-cased code (`#error_release_drift`). Every `ERROR_*`/`WARNING_*` code in `issues.py` must keep a `### \`CODE\`` heading in `docs/benchmarks/leaderboard-submission.md`, with no emoji or extra words in that heading. Adding a code means adding a section; renaming one means updating both.
- The user guide's `## Validation errors` heading is linked from `docs/operations/cli-reference.md` (`#validation-errors`); keep it plain.
- Packing and offline eval must stay numerically identical; change outcome or metric derivation only in `trial_results.py`.

## Submit flow

1. Resolve metadata: `--submission <dir>` (edited `metadata.yaml` + `README.md` from `nika leaderboard template`) or `--name` + `--authors`, which generate both from the run's recorded model and agent framework.
2. Resolve the model record (`hf_model.py`, below). Stops before packing if an open-weight model cannot be identified.
3. Pack scores into `{result_dir}/{YYYYMMDD}_{slug}/` (or `--out`) and trajectories into the sibling `{YYYYMMDD}_{slug}_trajectories/`.
4. Validate both unless `--skip-validate` (local only; GitHub CI still validates scores).
5. Check `gh` and Hugging Face authentication. `--dry-run` stops here and needs no authentication.
6. Open the GitHub PR under `submissions/<release_version>/{YYYYMMDD}_{slug}/` with the `leaderboard-submit` label (the leaderboard repo requires it on submission PRs). Without push access, `github_cli.ensure_fork` creates or reuses a fork. Over HTTPS, git uses the `gh` credentials; over SSH, a working GitHub SSH key is required. With no git author configured, the commit uses the GitHub login.
7. Open the Hugging Face dataset PR (a Hub Discussion/PR) under `trajectories/<release_version>/{YYYYMMDD}_{slug}/` (same slug, no `_trajectories` suffix).
8. Print both PR URLs. The command never merges.

Partial failure: if the Hub upload fails after the GitHub PR opens (network, Hub permissions), the error prints the GitHub PR URL and a retry command with `--skip-github --out <original-scores-package-directory>`, so trajectories upload under the original package name even on a later date.

Flags: `--dry-run`, `--hf-model`, `--hf-revision`, `--proprietary`, `--draft`, `--skip-github`, `--skip-trajectories`, `--skip-validate`, `--out`, `--title`, `--body`, `--repo`, `--traj-repo`, `--release-source official|local`.

### Package naming

Directory name `YYYYMMDD_<framework>_<model>`, lowercase ASCII words joined by underscores (validated on `--out`). The framework is the public harness name, not the NIKA agent ID: `cli.claude` → `Claude Code` → `claude_code`; `cli.codex` → `Codex`; `cli.opencode` → `OpenCode`; `cli.dsh` → `DeepSeek Harness`; `byo.langgraph` → `ReAct` → `react`. The map is in `meta_input.py`. Example: `20261012_claude_code_gemma_4_31b_it`.

## Release identity and drift

Validation computes expected trials from the official task definitions: the `benchmark/releases/<version>/` files at Git tag `v<version>` in `sands-lab/nika` (for example `v0.2.0`). Each trial ID embeds a hash of its task definition, so a run made from a checkout whose release files differ from the tag cannot pass.

- `nika benchmark run --release` compares the checkout's release files with the tag and lists changed tasks, asking whether to continue before any trial runs.
- `submit` and `nika leaderboard validate` read the tagged files by default, so local pass implies CI pass. `--release-source local` checks against the checkout instead (offline only); CI rejects that result and the report carries `WARNING_LOCAL_RELEASE`.
- `release_check.py` reports `ERROR_RELEASE_DRIFT` once per task when all of a task's expected trials are missing but trials with the same task stem and a different hash are present, or when the release files at the run's recorded `nika_git_commit` define a task differently (for example, different ground truth). The error lists changed tasks and the official commit. Fix: rerun those tasks from the official definitions into the same result dir with `--no-resume --task-id <task>`.
- If the recorded commit is in neither the local repo nor GitHub, only trial IDs are compared and the report carries `WARNING_RUN_COMMIT_UNVERIFIED`.

The user guide intentionally does not tell users to check out the tag manually; how NIKA selects the official definitions for a run is not a committed user contract yet.

## Coverage and scoring

- Coverage is exact: `case_count × n_trials` finished slots, with both values from the release. Example for 0.2.0: test 85 × 3 = 255, dev 84 × 3 = 252. Runs limited by `--task-id` or a single trial fail unless the directory already holds every required trial.
- Agents submit `(resource_id, fault_type)` pairs; scoring uses set metrics on those pairs. Primary means use the fixed denominator `N = case_count × n_trials`; non-`scored` trials contribute 0. Full rules: [root-cause evaluation](../benchmarks/root-cause-evaluation.md#score-a-submission).
- `score_status` (in each trial's `run.json`): `scored`, `no_submission` (agent fault, counts as 0, do not rerun), `infra_error` (no submission and missing ground truth or env/LLM API/MCP failure evidence; auto-retried by resume and `--retry-passes`), `grading_error` (submission exists but ground truth or scorer failed; regrade, do not rerun the agent). Any `grading_error` or residual `infra_error` rejects the package with no threshold (`ERROR_GRADING_ERROR`, `ERROR_INFRA_ERROR`).

## Run-validity flags

`nika.evaluator.validity` derives per-trial flags from `messages.jsonl`, `submission.json`, `nika.jsonl`, and `run.json`, and recomputes them for older runs. `nika eval summary` prints per-flag counts and rates (`validity_counts`, `validity_rates` in JSON). Packing stores `validity_flags` in each trial `result.json` and `validity_counts` in `metrics.json`; `eval_metrics.json` also records them.

| Flag | Meaning | Finding |
| --- | --- | --- |
| `no_submission` | No readable `submission.json` with a `root_causes` list | `WARNING_RUN_VALIDITY` |
| `empty_report` | Frozen diagnosis report is empty or only opening narration before the first tool call | `WARNING_RUN_VALIDITY` |
| `empty_trace` | No model or tool interaction in `messages.jsonl` | `ERROR_EMPTY_TRACE` at any rate (blocks pack and validate) |
| `truncated_final_turn` | Last LLM turn of a phase hit the output limit (`finish_reason: length`, `stop_reason: max_tokens`, or output tokens equal to `max_tokens` with no text) | `ERROR_RUN_VALIDITY` above `--max-harness-rate` |
| `max_steps_exhausted` | Diagnosis hit `max_steps`; details record whether a wrap-up report exists | `WARNING_RUN_VALIDITY` |
| `fault_absent_at_cleanup` | A `fault_presence_recheck` before cleanup read `absent`: the agent likely mutated the network, or the fault self-healed | `WARNING_RUN_VALIDITY` |
| `infra_error` | Trial failed to start, lacks ground truth, or has `score_status=infra_error` | `ERROR_RUN_VALIDITY` above `--max-harness-rate` |

`HARNESS_FLAGS = (truncated_final_turn, infra_error)`; `DEFAULT_MAX_HARNESS_RATE = 0.05`. The `infra_error` flag is distinct from the `infra_error` score status: a trial can carry the flag without that status. Design intent: harness flags indicate a broken harness rather than agent skill, so a small rate is tolerated; warning flags reflect agent behavior and must stay in the scored package.

## Package layouts

### Scores package (GitHub)

```text
{YYYYMMDD}_{slug}/
  README.md
  metadata.yaml
  results/
    identity.yaml          # release version + split, run identity, trajectories_relpath
    metrics.json           # aggregates, status_counts, validity_counts, n_trials_present/expected
    rca_confusion.json     # multi-label GT→predicted edge counts
    trials/{trial_id}/result.json
```

Traces are never stored on GitHub; `identity.yaml` binds the release version and split and points at the Hugging Face trajectories path.

Per-trial `result.json` adds, besides metrics:

- `gt_fault_types`: unique `fault_type` values from the session's `ground_truth.json` `root_causes` (fallback `[problem]`).
- `predicted_fault_types`: unique `fault_type` values from `submission.json` `root_causes`, or `null` when the file is missing or has no pairs.
- `validity_flags`: run-validity flags, empty when clean.

`rca_confusion.json` aggregates multi-label `(gt, predicted)` edges across trials and records `n_missing_prediction` and `missing_prediction_trial_ids`.

### Trajectories package (Hugging Face)

```text
{YYYYMMDD}_{slug}_trajectories/          # local sibling written by submit
  README.md
  metadata.yaml
  identity.yaml                          # scores_package + trajectories_relpath
  trials/{trial_id}/
    run.json                             # session meta, task text, outcome
    messages.jsonl                       # agent diagnosis + submission transcript
    nika.jsonl                           # NIKA system/workflow events
    ground_truth.json                    # expected root causes
    eval_metrics.json                    # rule-based scores, token/step counters
    submission.json                      # required when outcome=success
```

Only these files are copied from each session directory (`TRAJECTORY_REQUIRED_FILES` + `TRAJECTORY_OPTIONAL_SUCCESS_FILE`). Any path containing a `TRAJECTORY_FORBIDDEN_NAME_FRAGMENTS` entry (packet captures `.pcap`/`.pcapng`, `llm_judge.json`, `sandbox_manifest.json`, `validation-*.json`) raises `ERROR_TRAJECTORY_FORBIDDEN_FILE`. Scores and trajectories must agree on identity, run, and trial set (`ERROR_PACKAGE_PAIR_MISMATCH`).

Maintainers merge Hub PRs after review:

```shell
hf discussions list nika-bench/nika-trajectories --type dataset
hf discussions merge nika-bench/nika-trajectories <N> --yes --type dataset
```

## Metadata and model records

`metadata.yaml` field guidance (the user guide shows the schema):

- `info.github`: the agent implementation, not a personal profile; `https://github.com/sands-lab/nika` for built-in agents.
- `info.site`, `info.report`: project site and paper; a benchmark-paper link should be explained in the README.
- `agent.framework`: public harness name; the run identity keeps the NIKA agent ID.
- `agent.model`: public model name without org prefix; it feeds the slug and the leaderboard catalog match, so reuse an existing `catalog/models.json` key spelling.
- `agent.tools`, `agent.skills`: what the run used; MCP server names come from `tools.allowed_mcp_servers`.
- `agent.optimization_methods`: harness optimizations (prompting, skills, multi-agent, GEPA-style search), not weight fine-tunes.
- `agent.extra`: official model repo, evaluated revision, serving precision; never a guessed revision.

`hf_model.py` writes `hf_repo`, `hf_revision`, `hf_revision_source` (`latest` | `specified`) to `agent.extra` for `llm_provider: custom`:

- Model served under its Hugging Face ID (vLLM default): repo inferred from the run's `model`, latest revision pinned at submit time and printed.
- `--hf-revision <commit|branch|tag>`: resolved and stored as a commit SHA.
- Alias (`vllm --served-model-name`) or Ollama library name (`gpt-oss:120b`): unidentifiable; submit stops before packing unless `--hf-model org/name` or `--proprietary` is passed.
- Vendor providers (`openai`, `anthropic`, `deepseek`): nothing recorded.

## Leaderboard-side processing (sands-lab/nika-leaderboard)

- After the scores PR merges, the leaderboard repo looks up model release dates and reference prices from public catalogs and opens a catalog PR for its maintainers. Until that merges, cost shows `pending`; models with no public quote show `n/a`. The required `model-metadata` check reports new models as `pending`, which is expected. Catalog format and sources are documented in that repo's README.
- Pricing: vendor API list prices for proprietary models; public hosted-inference quotes for open weights (a reference estimate, not self-hosted GPU cost). Each run uses the quote current on its package date; a **Current prices** toggle re-prices all runs at the latest quote.
- Token-priced total: `(input_tokens × input_rate + output_tokens × output_rate) / 1,000,000`, from packed token counts. Cost per run (one attempt at every task): `total_cost / reported_trials × case_count`. No cache discounts; no network emulation or GPU allocation costs.
- Chart names combine model, normalized harness, optimization methods, and skills; `agent.extra.<method>_variant` (for example `gepa_variant: d+s`) adds a variant. Repeated configurations add split and run identity. The detail page keeps the original `info.name`.

## Review checklists

### GitHub scores PR

- Official release run; local pack/validate passed.
- Required metadata filled; README describes the system.
- Code, project/report links, public harness name, and tools match the run.
- Open-weight model: `agent.extra.hf_repo` and `hf_revision` match the served model.
- `model-metadata` check passes (new model → `pending`); CI `validate` green.
- Path is `submissions/<version>/{YYYYMMDD}_{slug}/`.

### Hugging Face trajectories PR

- Path is `trajectories/<version>/{YYYYMMDD}_{slug}/`.
- Paired scores PR exists and is linked.
- Trial set matches `case_count × n_trials`.
- No secrets or absolute machine paths.
- Maintainer merges the Hub PR after review.
