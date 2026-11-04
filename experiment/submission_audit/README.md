# Submission-audit experiment

Measure how many reference audit issues the [`nika-submission-audit` skill](../../.agents/skills/nika-submission-audit/SKILL.md) recovers as it processes leaderboard submissions. The corpus contains the 21 published benchmark release 0.2.0 submissions, with 5,355 trajectories and [93 reference issues](reference_issues.yaml).

An audit issue describes a defect in the benchmark, harness, or submission run. Each method starts with generic checks and an empty learned memory. Confirmed issues become scoped signatures that later audits can match deterministically.

The experiment compares two methods:

* **Agent review** uses eight reviewer agents and a curator to read flagged trials and a random sample, then write issue files.
* **Scan pipeline** scans every trial three times, groups cited findings, adjudicates supported groups, and records confirmed issues. Cross-trial tables compare tasks, healthy runs, tools, and prompts. With a source checkout, the pipeline also verifies source contracts, initial briefs, routing boundaries, and bounded parser or capture behavior.

## Run the experiment

Prerequisites:

* A NIKA checkout with Python 3.12+, `uv`, and dependencies installed through the [installation guide](../../docs/operations/installation.md).
* The release 0.2.0 trajectories from [`nika-bench/nika-trajectories`](https://huggingface.co/datasets/nika-bench/nika-trajectories), including `trajectories/0.2.0`.
* An OpenAI-compatible chat endpoint serving Qwen3.8-27B-FP8 or a comparable model. The results below use Qwen3.8-27B-FP8 unless stated otherwise.
* For source verification, the submissions' recorded commits in the checkout. Isolated process probes also require Docker and the selected images cached by digest.

For an authenticated endpoint, set `OPENAI_API_KEY` in the command's environment.

Run these commands from the NIKA checkout. Choose a results directory outside the checkout so baseline agents cannot read the audit implementation.

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

### Agent-review baseline

The baseline also requires Claude Code and an upstream server exposing the Anthropic Messages API at `/v1/messages`. The proxy below normalizes messages for that API; it does not translate an OpenAI-only endpoint. These commands assume the server at `$URL` supports both APIs and accepts placeholder credentials. Use the server's credential instead of `dummy` when authentication is required.

Start the proxy, then run reviewers through it:

```bash
uv run python -m experiment.submission_audit.anthropic_proxy \
  --upstream "${URL%/v1}" --port 18790
```

In another terminal, set the same `TRAJ` and `RUNS`, then run:

```bash
uv run python -m experiment.submission_audit.replay \
  --trajectories "$TRAJ" --out "$RUNS/agents/date" \
  --reviewers 8 --packet-budget 6 --reviewer-turns 60 --curator-turns 120 \
  --agent-timeout 3000 --curator-timeout 3600 \
  --agent-cmd 'env ANTHROPIC_BASE_URL=http://127.0.0.1:18790 ANTHROPIC_API_KEY=dummy
    claude -p --bare --model Qwen3.8-27B-FP8 --output-format json
    --allowedTools "Bash(./audit:*)" Read Write Edit Glob Grep'
```

Score the resulting runs with the same mapper and reference list used for the pipeline.

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

## Results

### Completed Qwen evaluation

The [result summary](results.json) separates fixed-input comparisons from regenerated scans. The controlled comparison holds scans, group verdicts, and non-prompt table findings fixed over all 21 submissions. Both memories use one shared Qwen mapper cache. The independent scan condition generates all 16,065 trial/sample records again, then evaluates the retained pipeline in date order. Its subsequent component comparison reuses those fresh scans.

The commands above run the retained pipeline. Sharing `prepare` output alone does not reproduce the controlled comparison, which also reused non-prompt table findings. The summary reports collected measurements; it does not include the original response caches or generated issue memories.

| Condition | Recovered references | Recall | Issue files |
| --- | ---: | ---: | ---: |
| Fixed-input control | 72/93 | 0.774 | 533 |
| Fixed-input source-assisted pipeline | 78/93 | 0.839 | 544 |
| Pipeline with independently regenerated scans | 77/93 | 0.828 | 612 |

The controlled comparison gains six reference entries with no lost reference. They cover prompt contrasts, missing advertised skills, a fault-control help menu, initial-brief topology, a healthy environment's host-uplink route, and capture interface handling. Capture interface handling overlaps an existing failed-writer mechanism, so the six entries represent five additional root mechanisms.

All 612 final issue files have valid mappings. One failed startup-warning mapping used a separately recorded Qwen response containing the exact canonical reference title. This recovery preserves every valid original mapping and adds no recovered reference. The final score therefore uses conditional format recovery, rather than the original output protocol alone.

Sparse prompt comparisons increase affected-trial detection from 182 to 239 across the corpus, with 57 gains and no losses. The additional matches extend a known prompt-leak mechanism to another environment; the final reference count remains 77/93. Fully uniform prompts, unmatched commits, and single sparse observations do not trigger the rule.

Tool-identity evidence removes six unsupported missing-tool cards from a 617-card memory. All 611 remaining card contents are unchanged after normalizing issue IDs and provenance dates. The final sparse-prompt comparison adds one card.

A warm deterministic audit of the 544-issue controlled memory re-detects all 544 conditions across the corpus in 293 seconds, plus 1.7 seconds to load the memory, with no model calls. This is reuse of known detectors on observed trajectories. It does not measure the final 612-card memory or the cost of discovering unknown defects.

### Original four-order comparison

The original methods ran in date order and three shuffled orders, with seeds 1 to 3. These results use earlier recording and mapping conditions and are reported separately from the controlled comparison above.

| Method | References recovered after 1 / 5 / 10 / 15 / 21 submissions | Recall | Issues written |
| --- | --- | --- | --- |
| Agent review | 0 to 7 / 14 to 20 / 25 to 37 / 29 to 44 / 39 to 45 | 0.42 to 0.48 | 68 to 81 |
| Scan pipeline | 12 to 16 / 42 to 55 / 59 to 64 / 62 to 67 / 67 to 71 | 0.72 to 0.76 | 491 to 505 |

The pipeline orders share scans and verdicts; the baseline orders use independent agent runs. Eleven changed pipeline files were mapped through Cursor Auto when Qwen was unavailable. These conditions prevent attributing differences between this table and the later Qwen evaluation entirely to the implementation.

On held-out labelled groups, the original adjudication evaluation gave precision 0.58 and recall 0.52, from a candidate pool containing about 25% real groups. This is a component evaluation, not the precision of the final issue memory.

## Limits

* The 21 submissions informed development. Fresh scans measure replication on those trajectories, not generalization to an untouched benchmark release or defect family.
* The reference set and mapping validation were checked by one person. A model mapping does not verify the finding's causal explanation.
* A Qwen evidence screen of 16 cards sampled from the 611-card memory found one supported claim, eight overclaimed descriptions, and seven with insufficient evidence. Bounded evidence and unresolved source checks prevent interpreting these counts as population precision.
* The final memory contains 612 cards for 77 recovered references. Duplicate descriptions and findings outside the reference set remain; reference recall alone does not measure their quality.
* The audit and mapper use the same model. A fresh mapper call can change assignments, so component comparisons must preserve mappings for unchanged inputs.
* Complete first-audit cost, independently reviewed final-memory precision, and performance on unseen data remain unmeasured.
* These measurements precede code cleanup and subsequent correctness fixes. Those fixes have regression coverage rather than a new model evaluation.
