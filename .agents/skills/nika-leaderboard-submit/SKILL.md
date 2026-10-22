---
name: nika-leaderboard-submit
description: Submit a finished official NIKA release run to the leaderboard (GitHub scores PR + Hugging Face trajectories PR) with complete metadata, a pinned open-weight model, and verified receipts. Use when the user asks to submit, publish, or upload benchmark results to the NIKA leaderboard.
---

# NIKA leaderboard submission

Turn a finished official release run into a reviewed leaderboard entry without
changing its results. `nika leaderboard submit` packs, validates, and uploads;
this skill supplies what the command cannot infer and checks the outcome.

The submitter does **not** add model release dates or prices. The leaderboard
repository resolves them after the scores PR merges; until then the entry shows
`pending`. Read [the submission guide](../../../docs/benchmarks/leaderboard-submission.md)
for the full package contract.

## Rules

* Never edit, rerun, drop, or re-score trials to change outcomes. Agent failures
  and timeouts stay in the package and count as zero.
* Never invent facts: revisions, serving precision, budgets, or links you have
  not read from the run, the user, or a public page. Write "not recorded" instead.
* Never print or commit tokens, `.env` contents, or absolute machine paths.
* Show the user the dry-run summary and get explicit approval before the real
  submit; it opens public PRs. Never merge either PR.

## 1. Read the run

From `<result_dir>/run.json`, record: `official` (must be `true`), `version`,
`split`, `agent_type`, `model`, `llm_provider`, `max_steps`, `reasoning_effort`,
`case_timeout_sec`, `nika_git_commit`, `nika_git_dirty`, `run_id`.

Stop and report if `official` is not true. Note a dirty NIKA tree in the README.

Check the same run was not already submitted:

```shell
gh api repos/sands-lab/nika-leaderboard/contents/submissions/<version> -q '.[].name'
```

Open any entry with the same framework and model and compare its
`results/identity.yaml` `run_id`. A resubmission of the same run is a metadata
correction; ask the user before continuing.

## 2. Identify the model

| `llm_provider` | Model kind | What to pass |
|---|---|---|
| `openai`, `anthropic`, `deepseek` | Vendor API | Nothing |
| `custom`, `model` is a Hugging Face ID (vLLM default) | Open weights | Nothing; latest revision is pinned |
| `custom`, alias or Ollama name (`gpt-oss:120b`) | Open weights | `--hf-model org/name` |
| `custom`, proprietary model behind a proxy or router | Proprietary | `--proprietary` |

Ask the user which revision the server loaded. Pass `--hf-revision` when they
know it (vLLM `--revision`, or the snapshot hash in the served path). Otherwise
`submit` records the repo's latest revision at submit time; say so in the
README. Do not pass a revision you guessed.

## 3. Write the submission

```shell
uv run nika leaderboard template -o <result_dir>/submission
```

Fill `metadata.yaml`:

* `info.name`: `<Harness> + <Model>`, e.g. `ReAct + Qwen3.6 35B A3B`.
* `agent.model`: the public model name without the org prefix, e.g.
  `Qwen3.6-35B-A3B-NVFP4` or `claude-haiku-5-5`. It sets the package slug and
  matches the leaderboard catalog by slug, so reuse the spelling of an existing
  catalog key when one exists (`catalog/models.json` in the leaderboard repo).
  The exact runtime ID stays in the run identity.
* `agent.framework`: the public harness, not the NIKA agent ID: `cli.claude` →
  `Claude Code`, `cli.codex` → `Codex`, `byo.langgraph` → `ReAct`.
* `info.github`: the agent implementation (`https://github.com/sands-lab/nika`
  for built-in agents), not a personal profile.
* `agent.tools`, `agent.skills`, `agent.optimization_methods`: what the run
  actually used; empty lists when none.
* Leave `hf_repo` and `hf_revision` out of `agent.extra`; `submit` writes them.

Write `README.md` with these sections, using numbers from the dry run (step 4):

1. One paragraph: harness, NIKA agent ID, model, and how it was served.
2. **Evaluation:** release, split, task count × trials; finished, scored, and
   agent-failed slots with the reason for failures; mean RCA F1, localization
   F1, detection score; step limit, output-token limit, and timeouts.
3. **Run provenance:** NIKA commit (and dirty state), engine and version,
   Hugging Face revision and serving precision, or "not recorded".
4. **Links:** agent code, project site, benchmark paper, model page,
   trajectories (`https://huggingface.co/datasets/nika-bench/nika-trajectories/tree/main/trajectories/<version>/<package>`).

Mention prices only to say the leaderboard estimates them from reference rates;
subscription or self-hosting costs are not the reference price.

## 4. Dry run and review

```shell
uv run nika leaderboard submit --result_dir <result_dir> \
  --submission <result_dir>/submission --dry-run [model flags from step 2]
```

Fix every error and rerun. Then check the packed scores package:

* `results/metrics.json`: `n_trials_present == n_trials_expected` (255 for
  0.2.0 test, 252 for dev); `status_counts` has zero `grading_error` and
  `infra_error`. Copy the metrics into the README.
* `metadata.yaml`: `agent.extra.hf_repo` / `hf_revision` name the served model,
  or are absent for proprietary models.
* Package directory is `YYYYMMDD_<framework>_<model>`.
* README numbers match `metrics.json`, and no placeholder text remains.

Summarize the package name, destinations, model record, and metrics for the
user and ask for approval.

## 5. Submit and verify

Run the same command without `--dry-run`. Add `--draft` if the user wants it.
If the GitHub PR opened but the Hugging Face upload failed, rerun with
`--skip-github --out <scores package directory>` exactly as the error prints.

Verify both receipts:

```shell
gh pr view <url> --json state,files -q '.state, .files[].path'
gh pr checks <url> --watch
uv run hf discussions list nika-bench/nika-trajectories --type dataset
```

Both `validate` and `model-metadata` must pass; a new model is reported as
`pending` by `model-metadata`, which is expected. Report both PR URLs, check
results, and any remaining blocker. Maintainers merge.
