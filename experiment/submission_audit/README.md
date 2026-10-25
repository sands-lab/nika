# Submission-audit experiment

This experiment measures how many known problems in the 21 published 0.2.0 leaderboard submissions an audit finds, and how quickly it finds them as it audits one leaderboard submission after another. It compares two ways of running the [`nika-submission-audit` skill](../../.agents/skills/nika-submission-audit/SKILL.md).

The problems are [audit issues](../../GLOSSARY.md): confirmed defects in the benchmark, its harness, or the runs of a leaderboard submission, such as a shortcut that reveals the injected failure or a tool that always fails. Each audit keeps the audit issues it confirms in its own *issue memory*, a directory of issue files with signatures that later audits match without an LLM. Both methods start with an empty memory: only the skill's generic checks, no issues learned from earlier audits.

* **Agent review** (`replay.py`, the baseline). For each leaderboard submission, eight reviewer agents read the trials that the generic checks flagged plus a random sample, and a curator agent writes issue files.
* **Scan pipeline** (`evolve.py`, the skill's `review` command):
  1. An LLM reads every trial several times (scan samples) and answers one fixed question per issue class, citing the step it relies on.
  2. Findings are grouped across trials.
  3. An adjudication call judges each group seen in two or more trials, or by two or more scan samples: a real problem, intended behaviour, or the audited agent's own mistake.
  4. Real groups become issue files without an LLM: the signature is the text their excerpts share.
  5. Per-environment tables of tasks, healthy runs, and tools are read the same way, so problems that only show across trials (a fault no configuration can see, a label always answered as another) become statistical signatures.

## Files

| File | Purpose |
| --- | --- |
| `reference_issues.yaml` | The 93 audit issues confirmed so far in the 21 leaderboard submissions, each with the submissions it appears in |
| `adjudication.yaml` | Verdicts on issues that earlier audit runs wrote and that matched no reference issue; the confirmed ones joined the reference set |
| `issue_refs.yaml` | Which issues of the skill record which reference issues; labels trial steps for `scan_eval.py` |
| `labels/scan_groups.yaml` | 120 scan groups labelled by independent reviewers, for `adjudicate_eval.py` |
| `replay.py` | Agent-review baseline |
| `evolve.py` | Scan pipeline: `prepare` scans and adjudicates every leaderboard submission in parallel; `replay` grows the memory in one order |
| `map_issues.py` | Maps every written issue to the reference issues with one LLM, the same way for every run |
| `recall.py` | Recall of each run against the reference issues |
| `scan_eval.py` | Scans the first leaderboard submission of each run and scores the findings by the steps they cite |
| `adjudicate_eval.py` | Adjudication against the labelled groups |
| `skill.py` | Puts the skill's `auditlib` on the import path |
| `anthropic_proxy.py` | Lets Claude Code, which drives the baseline agents, talk to a vLLM endpoint |

## Run it

Prerequisites:

* The 0.2.0 trajectories from the [`nika-bench/nika-trajectories`](https://huggingface.co/datasets/nika-bench/nika-trajectories) dataset (`trajectories/0.2.0`), referred to as `$TRAJ`.
* An OpenAI-compatible chat endpoint, referred to as `$URL`. The results below used Qwen3.8-27B-FP8 on vLLM unless a row says otherwise.
* For the baseline, Claude Code, and a run directory `$RUNS` outside the NIKA checkout, because the agents are denied read access to the checkout.

Neither a trial's scan nor a group's verdict depends on the memory, so `prepare` does that work once, in parallel, for all orders. The per-environment tables include earlier leaderboard submissions, so they are read in `replay`.

```bash
# Scan pipeline: scans and verdicts for every leaderboard submission
uv run python -m experiment.submission_audit.evolve prepare \
  --trajectories $TRAJ --base-url $URL --model Qwen3.8-27B-FP8 --prepared runs/prepared

# Memory in one order; repeat with --order date and --shuffle-seed 2 and 3
uv run python -m experiment.submission_audit.evolve replay \
  --trajectories $TRAJ --prepared runs/prepared --order shuffle --shuffle-seed 1 \
  --base-url $URL --model Qwen3.8-27B-FP8 --out runs/pipeline/shuffle1

# Agent-review baseline, through the proxy
uv run python -m experiment.submission_audit.anthropic_proxy --upstream ${URL%/v1} --port 18790 &
uv run python -m experiment.submission_audit.replay \
  --trajectories $TRAJ --order shuffle --shuffle-seed 1 --out $RUNS/agents/shuffle1 \
  --reviewers 8 --packet-budget 6 --reviewer-turns 60 --curator-turns 120 \
  --agent-timeout 3000 --curator-timeout 3600 \
  --agent-cmd 'env ANTHROPIC_BASE_URL=http://127.0.0.1:18790 ANTHROPIC_API_KEY=dummy
    claude -p --bare --model Qwen3.8-27B-FP8 --output-format json
    --allowedTools "Bash(./audit:*)" Read Write Edit Glob Grep'

# Score any set of runs the same way
uv run python -m experiment.submission_audit.map_issues \
  --runs runs/pipeline --base-url $URL --model Qwen3.8-27B-FP8
uv run python -m experiment.submission_audit.recall --runs runs/pipeline --out recall.json
```

## Scoring

A run *recovers* a reference issue once it writes an issue that the mapper maps to it. `map_issues.py` shows one LLM the reference issues and one written issue at a time and asks which reference issues describe the same defect; the written issue must name the artifact, environment, tool, or fault the reference issue names. Against the hand-checked mapping of the baseline runs it agrees with precision 0.76 to 0.83 and recall 0.77 to 0.82. Recall is the share of the 93 reference issues a run recovers.

## Results

Both methods ran in the same four orders: date order and three shuffles (seeds 1 to 3).

| Method | Reference issues recovered after 1 / 5 / 10 / 15 / 21 leaderboard submissions | Recall | Issues written |
| --- | --- | --- | --- |
| Agent review | 0 to 7 / 14 to 20 / 25 to 37 / 29 to 44 / 39 to 45 | 0.42 to 0.48 | 68 to 81 |
| Scan pipeline | 13 to 16 / 43 to 55 / 60 to 65 / 62 to 68 / 67 to 71 | 0.72 to 0.76 | 595 to 613 |

* The scan pipeline recovers about as many reference issues after five leaderboard submissions as agent review does after all 21.
* Problems that only show across trials are recovered. Over the four runs the scan pipeline recovers 7 of 7 injected failures that leave no observable symptom, 5 of 5 fault types that agents systematically confuse with another, and the 1 grading error; agent review recovers 5, 3, and 0.
* The four scan-pipeline orders share the scans and verdicts of `prepare`, so their agreement (64 reference issues recovered in every order) overstates how stable the method is. The baseline orders ran independent agents.
* The scan pipeline writes about eight times as many issues. 58% to 61% of them map to a reference issue, against 62% to 70% for agent review; much of the volume is one issue per instance of agent behaviour, such as each state-changing command an agent ran.
* The runs above wrote an issue for every state-changing command the scan reported. The pipeline now treats such a group as recorded when a built-in check whose signature identifies the instance (`AGENT-STATE-MUTATION`, `POLICY-INIT-UNLISTED-TOOLS`) fires on its cited step. Applied to the stored memories of the four orders, this removes 104 to 119 issues per order (leaving 490 to 502) and no recovered reference issue. The four orders were not rerun with the rule.

**First leaderboard submission of each run.** `scan_eval.py` scans each run's first leaderboard submission with an empty memory. The four leaderboard submissions together contain 148 (reference issue, leaderboard submission) pairs for which `issue_refs.yaml` labels the steps that show the issue. A pair is reached when a finding cites one of its labelled steps with a verbatim excerpt.

| Scan | Pairs reached | Pairs reached by groups judged real |
| --- | --- | --- |
| Agent review, raw reviewer findings | 16 | |
| Qwen3.8, one scan sample per trial | 102 to 105 | |
| Qwen3.8, three scan samples per trial | 113 | 91 (659 of 2660 groups kept) |
| Composer 2.5 through `--cli cursor`, one scan sample per trial | 128 | 81 (356 of 4403 groups kept) |

For the 113 pairs that the three-sample Qwen scan reached, independent reviewers checked whether a citing finding describes the issue itself; it did for 91 of them.

**Adjudication.** On the labelled groups of the held-out leaderboard submissions (`adjudicate_eval.py`), about 25% of groups are real before adjudication. Keeping the groups judged real gives precision 0.58 and keeps 52% of the real groups. Adding environment design notes, a majority over three calls, or a reworded definition of a lure (fault-like behaviour of a healthy environment that its brief does not state) changed these numbers within noise.

## Limitations

* One person checked the reference issues and the mapper's validation set.
* The audits and the mapper use the same model, so a mapper error affects both methods alike.
* The full runs cover one release and one model. Composer covered the first leaderboard submissions only, with one scan sample per trial.
* The baseline runs gave the agents the earlier, method-only text of the skill; its method is the same as the reviewer, skeptic, and curator guides.
