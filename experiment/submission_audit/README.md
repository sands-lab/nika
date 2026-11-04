# Submission-audit experiment

Measure how many reference audit issues the [`nika-submission-audit` skill](../../.agents/skills/nika-submission-audit/SKILL.md) recovers as it processes leaderboard submissions. The corpus contains the 21 published benchmark release 0.2.0 submissions, with 5,355 trajectories and [93 reference issues](reference_issues.yaml).

An audit issue describes a defect in the benchmark, harness, or submission run. Each method starts with generic checks and an empty learned memory. Confirmed issues become scoped signatures that later audits can match deterministically.

The pipeline scans every trial three times, groups cited findings, adjudicates supported groups, and records confirmed issues. Cross-trial tables compare tasks, healthy runs, tools, and prompts. With a source checkout, it also verifies source contracts, initial briefs, routing boundaries, and bounded parser or capture behavior.

## Run the experiment

Prerequisites:

* A NIKA checkout with Python 3.12+, `uv`, and dependencies installed through the [installation guide](../../docs/operations/installation.md).
* The release 0.2.0 trajectories from [`nika-bench/nika-trajectories`](https://huggingface.co/datasets/nika-bench/nika-trajectories), including `trajectories/0.2.0`.
* An OpenAI-compatible chat endpoint serving Qwen3.8-27B-FP8 or a comparable model. The results below use Qwen3.8-27B-FP8 unless stated otherwise.
* For source verification, the submissions' recorded commits in the checkout. Isolated process probes also require Docker and the selected images cached by digest.

For an authenticated endpoint, set `OPENAI_API_KEY` in the command's environment.

Run these commands from the NIKA checkout. Choose a results directory outside the checkout to keep generated artifacts separate from source.

```bash
TRAJ=/path/to/nika-trajectories/trajectories/0.2.0
URL=http://127.0.0.1:8000/v1
RUNS=/path/to/audit-results

uv run python -m experiment.submission_audit.evolve prepare \
  --trajectories "$TRAJ" --prepared "$RUNS/prepared" \
  --base-url "$URL" --model Qwen3.8-27B-FP8

uv run python -m experiment.submission_audit.evolve replay \
  --trajectories "$TRAJ" --prepared "$RUNS/prepared" \
  --out "$RUNS/pipeline/date" --repo . \
  --base-url "$URL" --model Qwen3.8-27B-FP8

uv run python -m experiment.submission_audit.map_issues \
  --runs "$RUNS/pipeline" --base-url "$URL" --model Qwen3.8-27B-FP8
uv run python -m experiment.submission_audit.recall \
  --runs "$RUNS/pipeline" --out "$RUNS/recall.json"
```

`prepare` computes scans and group verdicts once. `replay` grows the issue memory in date order and computes cross-trial and source-assisted findings. For shuffled orders, add `--order shuffle --shuffle-seed <seed>` and choose a separate output directory. Shared prepared inputs isolate memory effects; they do not provide independent scan replications.

Successful mapping writes `mapping_llm.yaml` inside each run directory. The final command writes the recovery counts and curves to `recall.json`.

## Scoring and evidence

A run recovers a reference issue when it writes an issue describing the same defect and the mapper assigns that reference ID. The written issue must identify the relevant artifact, environment, tool, fault, or component. Recall is the number of recovered references divided by 93.

The mapper caches complete inputs across runs so unchanged issues keep the same assignment. Malformed, incomplete, and invalid-reference replies remain failures. New attempts are retained in each run's `mapping-calls.jsonl`; an incomplete mapping cannot receive a final score. Historical mapper validation against hand-checked baseline mappings gave precision 0.76 to 0.83 and recall 0.77 to 0.82. These measure mapping agreement, not audit precision.

| File | Purpose |
| --- | --- |
| [reference_issues.yaml](reference_issues.yaml) | Reference defects and the submissions in which they appear |
| [issue_refs.yaml](issue_refs.yaml) | Aliases used to locate candidate evidence for scan evaluation |
| [labels/scan_groups.yaml](labels/scan_groups.yaml) | 120 reviewed groups for adjudication evaluation |
| [adjudication.yaml](adjudication.yaml) | Review of earlier unmatched issues |
| [scan_eval.py](scan_eval.py) | Coverage of labelled evidence steps |
| [adjudicate_eval.py](adjudicate_eval.py) | Judgment quality on labelled groups |

A labelled-step match locates potential evidence; it does not establish that a finding explains the defect. Deterministic checks, candidate interpretation, successful recording, and reference mapping are separate stages.

## Result

The [completed evaluation](results.json) recovers **77/93 reference issues (82.8%)** from independently regenerated scans of all 21 submissions. The scan pool contains 16,065 valid trial/sample records. The retained pipeline processes them in date order and writes 612 issue files, all with valid mappings. Subsequent component comparisons reused that scan pool.

One failed startup-warning mapping used a separately recorded Qwen response containing the exact canonical reference title. This conditional format recovery preserves every valid original mapping and adds no recovered reference. It is part of the reported scoring protocol.

The [verified experiment archive](https://drive.google.com/drive/folders/1n-uHxY8kP0MvI9o2HtSKb4UfnXLFF23L) contains the original issues, mapper answers, trajectories, evaluated source, and the controls and ablations supporting the method. Download and extract `recall-reproduction.tar.zst`, then run `python3 reproduce.py` to recompute recall from saved model answers without a GPU or endpoint. The archive keeps earlier conditions separately labelled; they are not alternative headline results.

The commands above run the current implementation. They do not reproduce the exact saved model responses. A new stochastic model run can produce different findings and mappings.

## Limits

* The 21 submissions informed development. Fresh scans measure replication on those trajectories, not generalization to an untouched benchmark release or defect family.
* The reference set and mapping validation were checked by one person. A model mapping does not verify the finding's causal explanation.
* A Qwen evidence screen of 16 cards sampled from the 611-card memory found one supported claim, eight overclaimed descriptions, and seven with insufficient evidence. Bounded evidence and unresolved source checks prevent interpreting these counts as population precision.
* The final memory contains 612 cards for 77 recovered references. Duplicate descriptions and findings outside the reference set remain; reference recall alone does not measure their quality.
* The audit and mapper use the same model. A fresh mapper call can change assignments, so component comparisons must preserve mappings for unchanged inputs.
* Complete first-audit cost, independently reviewed final-memory precision, and performance on unseen data remain unmeasured.
* These measurements precede code cleanup and subsequent correctness fixes. Those fixes have regression coverage rather than a new model evaluation.
