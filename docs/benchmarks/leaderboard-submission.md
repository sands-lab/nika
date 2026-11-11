# 🏆 Leaderboard submission

Publish the result of an official benchmark release run on the NIKA leaderboard. One command packs the run, validates it, and opens two pull requests:

| Package | Contents | Destination |
| --- | --- | --- |
| **Scores** | Metrics for every trial | GitHub PR to [`sands-lab/nika-leaderboard`](https://github.com/sands-lab/nika-leaderboard) |
| **Trajectories** | Each trial's agent transcript and events | Hugging Face dataset PR to [`nika-bench/nika-trajectories`](https://huggingface.co/datasets/nika-bench/nika-trajectories) |

## Before you start

- A finished `nika benchmark run --release …` against a current (non-deprecated) benchmark release.
- Authenticated [`gh`](https://cli.github.com/) access (`gh auth login`, or `GH_TOKEN` with `repo` scope).
- Hugging Face authentication through `hf auth login`, or `HF_TOKEN` in the environment or the repository-root `.env`. The token must permit proposing dataset changes.

> [!IMPORTANT]
> The run must include every trial the release defines for its split. Agent failures and timeouts count as finished trials with a zero score. Retry infrastructure failures. For grading errors, repair the grading inputs and regrade the saved submissions.

**Terms** (see the [glossary](../../GLOSSARY.md)):

- **Benchmark release**: a published, unchanging set of **tasks**, such as `0.2.0`, split into Dev and Test sets.
- **Trial**: one scored run of a task. The release defines how many trials each task needs.
- **Scores package** and **trajectories package**: the two submissions above.
- **`metadata.yaml`**: the entry's name, authors, model, and agent harness. See [its fields](#metadatayaml).

## 🚀 Submit

```shell
uv run nika leaderboard submit --result_dir results/my-run \
  --name "My Agent" --authors "Your Name"
```

The command:

- generates `metadata.yaml` and a short README from your name, your authors, and the model and agent framework recorded in the run;
- packs and validates both packages, then checks both accounts;
- opens the GitHub PR (labeled `leaderboard-submit`) and the Hugging Face PR, and prints both URLs.

Maintainers review and merge the PRs; the command never merges them.

> [!TIP]
> Add `--dry-run` to pack and validate locally without remote authentication. It prints both destination paths.

> [!NOTE]
> This form creates minimal metadata. Review the entry's code link, project links, and harness display name before publishing, or use custom metadata.

## ✏️ Submit with custom metadata

To add a longer README, code links, tools, or skills, write the metadata yourself and pass `--submission` instead of `--name` and `--authors`:

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml and README.md

nika leaderboard submit --result_dir results/my-run \
  --submission results/my-run/submission
```

## Options

| Flag | Effect |
| --- | --- |
| `--dry-run` | Pack and validate without opening PRs |
| `--hf-model org/name`, `--hf-revision <commit\|branch\|tag>`, `--proprietary` | Identify the model; see [Record the open-weight model](#record-the-open-weight-model) |
| `--draft` | Open the GitHub PR as a draft |
| `--skip-github`, `--skip-trajectories` | Skip one of the two PRs |
| `--title`, `--body` | Set the GitHub PR title and body |
| `--repo`, `--traj-repo` | Target another scores repository or trajectories dataset |
| `--release-source local` | Check against this checkout's release files when offline; leaderboard CI still uses the official release |
| `--skip-validate` | Skip local checks only; GitHub CI still validates scores |

## ⚠️ If an upload fails

An upload can fail after the GitHub PR opens, for example because of a network error or missing Hub permissions. The error prints the existing PR URL. Retry with the flags the error prints:

```shell
nika leaderboard submit … --skip-github --out <original-scores-package-directory>
```

Trajectories upload under the original package name, even on another date. The directory name must be `YYYYMMDD_<framework>_<model>` in lowercase ASCII words separated by underscores. The framework is the public harness name: `cli.claude` becomes `claude_code`, and LangGraph becomes `react`.

---

# 📚 Reference

The rest of this page is reference material: the files you edit or that `submit` produces, how the leaderboard prices runs, and what to do when validation fails.

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

- `info.github`: the agent implementation or library, not the submitter's profile. For NIKA's built-in agents, use `https://github.com/sands-lab/nika`.
- `info.site` and `info.report`: the project site and any relevant paper or report. If you link the benchmark paper, say so in the README so readers can tell it from an agent report.
- `agent.framework`: the public harness name, such as `Claude Code`, `Codex`, `OpenCode`, `DeepSeek Harness`, or `LangGraph`. The run identity keeps the NIKA agent ID, such as `cli.claude`.
- `agent.tools` and `agent.skills`: what the agent used. Copy enabled MCP server names from the run's `tools.allowed_mcp_servers` when appropriate.
- `agent.optimization_methods`: short labels for harness optimizations applied around the model, such as `GEPA`, `skills`, or `Multi-agent`. Weight fine-tuning does not belong here.
- `agent.extra`: the official model repository, the evaluated revision when available, and serving precision or other configuration needed to reproduce the run. Do not guess a revision after the run.

`README.md` is a short description of the system, its authors, and links to code, report, and site.

## Model release dates, prices, and display names

You do not supply release dates or prices. The leaderboard adds them after your scores PR merges, and the website shows the cost as `pending` until then (`n/a` for models with no public quote).

### Record the open-weight model

Open-weight release dates and prices come from the model's Hugging Face repository, so `submit` writes the repository and a pinned commit to `agent.extra`:

```yaml
agent:
  extra:
    hf_repo: nvidia/Qwen3.6-35B-A3B-NVFP4
    hf_revision: 1355db6a052410cfd62085d94b58866fd0f2c3c5
    hf_revision_source: latest   # or: specified
```

When a custom endpoint served the model under its Hugging Face ID, as vLLM does by default, `submit` finds the repository from the run's `model` and needs no flags. It records the repository's latest revision at submit time and prints it. If the run served an older commit, branch, or tag, pass `--hf-revision`; `submit` stores its commit SHA.

`submit` stops before packing when it cannot identify the model, for example when vLLM used `--served-model-name` or Ollama served a library model such as `gpt-oss:120b`. Pass the repository explicitly, or mark a proprietary model behind a custom endpoint:

```shell
uv run nika leaderboard submit --result_dir results/my-run \
  --name "My Agent" --authors "Your Name" \
  --hf-model openai/gpt-oss-120b      # or: --proprietary
```

Runs against a vendor API (`openai`, `anthropic`, `deepseek`) record nothing.

### How the website prices runs

The leaderboard prices proprietary models at vendor API list prices and open-weight models at public hosted-inference quotes. For open weights, this is a reference estimate for hosted inference, not your self-hosted GPU bill. Each run uses the quote current on its package date; the **Current prices** toggle re-prices every run at the latest quote.

The token-priced total is `(input_tokens × input_rate + output_tokens × output_rate) / 1,000,000`, using token counts from the packed run. Cost per run is the cost of one attempt at every task: `total_cost / reported_trials × case_count`. The calculation applies no cache discounts and adds no network emulation or GPU allocation costs.

Chart names combine the model, normalized harness name, optimization methods, and skills. An optimization's `agent.extra.<method>_variant` field adds its variant, such as `gepa_variant: d+s`. Repeated configurations add the split and run identity to stay distinct. The submission detail page keeps your original `info.name`.

## What gets published

`submit` builds both packages from the result directory. You do not edit them.

- **Scores package (GitHub):** your `metadata.yaml` and README, the entry's run identity, and the metrics and result of every trial. It contains no transcripts.
- **Trajectories package (Hugging Face):** for every trial, the agent's transcript, the NIKA events, the ground truth, the scores, and the agent's final answer. Packet captures and judge artifacts are never published.

Both land under `<release_version>/{YYYYMMDD}_{slug}/`, in `submissions/` on GitHub and in `trajectories/` on Hugging Face. The `{slug}` combines the public harness and model names.

## 🔍 How validation works

### Audit recorded evidence

`submit` audits its paired trajectories during validation and includes `audit.json` in the scores package. A blocking verdict stops submission; warnings require review. You can repeat the same checks against a scores package and downloaded trajectories:

```shell
uv run nika leaderboard validate <scores-package> \
  --fetch-trajectories --audit-out audit.json --audit-repo .
```

The download selects the merged trajectories or their open Hub PR, pins its commit, and records that revision in the audit report. For local paired data, use `--trajectories <dir>`. Validation also discovers an existing sibling directory. If no trajectories are available, scores-only validation reports that the audit did not run.

The [reviewed knowledge base](../../src/nika/audit/kb_data/) defines blocking and warning findings.

### Automatic trajectory review

Leaderboard CI validates each changed entry against its paired trajectories and reviews the recorded agent behavior. New audits use the latest merged knowledge base. Model review requires a completed trajectory audit and no detected secrets in the scores package. Validation errors still fail the entry check even when model review runs.

Open the pull request's validation check to inspect the audit summary and download its evidence artifacts. The `audit.json` report lists known issues with trial and step references. Review the cited observations when addressing warnings or blocking findings; a successful CI job does not dismiss reported warnings.

Newly proposed audit rules require maintainer review before adoption. For reviewer setup and workflow maintenance, see [Run leaderboard submission audits](../development/testing.md#run-leaderboard-submission-audits).


`submit` runs the same checks as leaderboard CI. You need the details below only when a check fails. It verifies that:

- Identity and trial IDs match the official release, and the task definitions at the run's recorded NIKA commit match it too.
- Every trial the release defines is present, and the package metrics match the per-trial results. Trials that are not `scored` count as 0 in primary means.
- No trial ended with a scoring error. Each trial's `score_status` in its `run.json` is `grading_error` when grading failed, or `infra_error` when the environment, LLM API, or MCP failed. Resume the run to retry `infra_error` trials; for `grading_error`, repair the grading inputs and regrade (see [trial `score_status`](root-cause-evaluation.md#trial-score_status)).
- Each trial's transcript shows a real agent run, not a failed start or a truncated answer.
- The trajectories package has the required per-trial files and the same trial set.
- Package text contains no secrets or absolute paths.
- Paired per-trial metrics agree, and the deterministic trajectory audit checks recorded tool use, harness consistency, and known shortcuts.

[Root-cause ground truth and scoring](root-cause-evaluation.md#score-a-submission) describes per-trial metrics and aggregation.

An `empty_trace` or `fault_absent_at_cleanup` flag rejects the submission regardless of its rate. Packing and trajectory validation check the original trial artifacts; scores validation checks the packaged flags. Inspect the trajectory for network changes or a self-healing fault before rerunning an absent-fault trial. NIKA preserves the raw scores for analysis. The `truncated_final_turn` and `infra_error` validity flags reject validation when either flag's rate exceeds `--max-harness-rate` (default 5%). The `no_submission`, `empty_report`, and `max_steps_exhausted` flags only warn.

## Validation errors

Each finding has a stable code, a cause, and a fix. To check a packed scores package as leaderboard CI does:

```shell
uv run nika leaderboard validate results/my-run/20261027_codex_gpt_6_astra
```

`--format json` prints a machine-readable report and `--format markdown` prints a pull request comment section; both include the release commit the package was checked against. The command exits with status 1 when the package has errors. For offline checks, `--release-source local` uses this checkout's release files; leaderboard CI independently checks the official reference.

### `ERROR_FAULT_ABSENT`

The injected fault disappeared before cleanup, so the trial cannot qualify for submission. Inspect its trajectory for network changes or a self-healing fault, repair the cause, and rerun the task. Keep the original run for analysis. The harness failure-rate allowance does not apply to this error.

### `ERROR_RELEASE_DRIFT`

The package was produced with task definitions that differ from the official release, usually because the NIKA checkout used for the run is older or newer than the release. The error lists the changed tasks and the official commit. Fetch that commit and create a separate checkout before rerunning. Use the reported commit for legacy releases too; they have no release tag. Install that checkout's dependencies and restore the original agent implementation. Prepare a compatible run config with the original model revision, provider, serving and harness settings, including any options previously supplied on the command line. Pass its absolute path as `<original-run-config>` below, configure the original provider's credentials, and run into a new result directory. Keep the original results:

```shell
git fetch origin <official-commit>
git worktree add <release-checkout> <official-commit>
cd <release-checkout>
uv sync
uv run nika benchmark run --release <version> --split <split> \
  --run-config <original-run-config> --result_dir <new-results>
```

### `ERROR_TRIALS_MISSING`

Some task × trial slots of the release have no result. Resume the run until every trial finishes, then pack again.

### `ERROR_TRIALS_UNEXPECTED`

The package contains trials that the release does not define, for example from another split or an ad-hoc run. Pack again from the release run directory.

### `ERROR_RELEASE_UNAVAILABLE`

NIKA could not read the official release. If GitHub is unreachable, restore connectivity and retry. If the version has no published reference, check that you selected the intended version; maintainers must publish its reference before official submission is available.

### `ERROR_BENCHMARK_MISMATCH`

The package identity or the release settings at the run's recorded NIKA commit differ from the official release. The error lists the differences. Follow [release-drift recovery](#error_release_drift) to rerun the original agent and model configuration against the official definitions, then pack that run.

### `ERROR_SCORE_MISMATCH`

The package metrics differ from the aggregate NIKA recomputes from the per-trial results. Pack again instead of editing package files.

### `ERROR_GRADING_ERROR`

Some trials have `score_status=grading_error`. Repair the grading inputs and [regrade the saved submissions](root-cause-evaluation.md#run-offline-eval), then pack again. Keep the recorded agent answers:

```shell
uv run nika eval metrics --result_dir results/my-run
```

### `ERROR_INFRA_ERROR`

Some trials still have `score_status=infra_error` after the run's retries. Resume the run until none remain.

### `ERROR_RUN_VALIDITY`

More than `--max-harness-rate` of trials (default 5%) show a harness problem rather than an agent mistake: the agent's last reply was cut off at the output token limit, or the trial failed to start or lacks ground truth. Fix the harness configuration, such as the output token limit, and rerun the affected trials.

### `ERROR_UNOFFICIAL_RUN`

The run was not started with `nika benchmark run --release`, so `run.official` is false.

### `ERROR_SECRET_FOUND`

Package text contains a possible credential or an absolute host path. Remove the value from `metadata.yaml`, `README.md`, or the run artifacts it came from, and pack again.

### `ERROR_TRAJECTORY_AUDIT`

Recorded trajectory evidence triggered a blocking audit rule. Inspect the audit report's cited trials and steps, repair the cause, and rerun affected trials.

### `WARNING_TRAJECTORY_AUDIT`

Provide the paired trajectories if the audit did not run. Otherwise, review the report's cited observations. Warnings do not block submission.

### `ERROR_PACKAGE_INCOMPLETE`

A required package file or directory is missing, or the package contains symbolic links. Pack again from the original result files with `nika leaderboard submit --dry-run` and the original metadata options.

### `ERROR_METADATA_INVALID`

`metadata.yaml` misses a required field, has an unknown field, or does not parse. Compare it with `nika leaderboard template`.

### `ERROR_IDENTITY_INVALID`

`identity.yaml` does not match the package schema, usually because it was edited by hand or written by a NIKA version with another schema. Pack again.

### `ERROR_RESULTS_INVALID`

A metrics, RCA confusion, or per-trial result file does not match the package schema, or a trial directory name differs from its trial ID. Pack again.

### `ERROR_TRAJECTORY_INVALID`

A trajectory trial lacks one of its [published files](#what-gets-published), or a JSON Lines file does not parse. Pack again.

### `ERROR_EMPTY_TRACE`

A trial's `messages.jsonl` records no model or tool interaction, usually because the agent failed to start or its harness did not record the trace. The error lists the affected trials and rejects the package at any rate. Fix the agent or harness setup, rerun those trials with `--no-resume --task-id <trial id>`, and pack again.

### `ERROR_TRAJECTORY_FORBIDDEN_FILE`

The trajectories package contains packet captures, validation dumps, or judge artifacts, which are not published. Pack again.

### `ERROR_PACKAGE_PAIR_MISMATCH`

The scores and trajectories packages differ in benchmark identity, run, or trial set. Pack both in one `nika leaderboard submit` call.

### `WARNING_LOCAL_RELEASE`

The package was checked against this checkout's release files (`--release-source local`). Validate without that option before submitting.

### `WARNING_RUN_COMMIT_UNVERIFIED`

The NIKA commit recorded in the package is neither in the local repository nor on GitHub, so NIKA compared trial IDs only. Run from a commit that is on GitHub, or push the commit the run used.

### `WARNING_RUN_VALIDITY`

Some trials show signs of a problem that does not block the submission. Review trials with no readable `submission.json`, an empty diagnosis report, or the step limit exhausted. An absent fault blocks submission under [`ERROR_FAULT_ABSENT`](#error_fault_absent).

### `WARNING_NON_DEFAULT_PRIMARY`

The release declares a primary metric other than `rca_f1`. No action is needed when the release defines it.
