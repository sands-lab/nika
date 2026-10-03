# Red team injection-trace audit

This experiment checks whether NIKA fault injections leave traces that give away a case's answer. Examples are shell history, staging files, or manifests that name the faulty device or the fault. It is for NIKA maintainers who change failure injection or the runtime exec path.

The red team is the evaluated agent itself. `redteam.py` runs `nika benchmark run` with the normal cli.claude harness, MCP tools, sandbox, and submission phase. It swaps only the diagnosis system prompt and the task text (`prompt.md`). The agent does not diagnose. It reports each trace with the tool calls that show it and the answer the trace implies. Its `best_guess` goes through the normal `submit()` and scorer, which gives the shortcut success rate.

## Judge

A deterministic judge (`redteam.py judge`) gives every reported trace one verdict:

| Verdict | Condition |
| --- | --- |
| `unsupported` | The excerpt does not occur in the cited tool call's output. |
| `self_inflicted` | Every excerpt line is a command the agent itself ran earlier in the trial. |
| `wrong_answer` | The implied fault type and device both miss `ground_truth.json`. |
| `in_baseline` | Replaying the cited calls on a fresh fault-free deployment reproduces the excerpt (timestamps masked). |
| `baseline_pending` | The cited calls were not replayed yet, or were skipped because they match the mutating-command filter. |
| `confirmed` | Verified citation, matching answer, absent from the healthy baseline. |

Review each `confirmed` trace by hand. Some are the fault mechanism itself (a `stress-ng` process, an AllowedIPs line), not a leftover of the injection. Only leftovers are injection bugs.

## Prerequisites

- A NIKA checkout with its own venv (`uv sync`) and a repo-root `.env`.
- The model endpoint from `config.yaml` (Qwen3-30B-A3B-Instruct-2507 on vLLM). `baseline` and `run` stop in preflight if it is down.
- At least 16 GB of free memory (`RED_TEAM_MIN_FREE_GB`).
- A `runs/` directory next to `redteam.py`. It is gitignored. Make it a symlink to wherever results should live.

Run only one `redteam.py` command at a time. Each trial and replay deploys a lab, and `config.yaml` runs one trial at a time.

## Run an audit

Run every command from the repo root with `.venv/bin/python`, not `uv run`, so the venv is not re-synced:

```bash
python experiment/red_team/redteam.py sample NAME --max-class light --with-healthy
python experiment/red_team/redteam.py run NAME --phase before
python experiment/red_team/redteam.py baseline NAME --phase before
python experiment/red_team/redteam.py summary NAME
```

1. `sample` writes `runs/NAME/cases.yaml`: the cheapest case of each fault type up to `--max-class`, plus one healthy case per sampled environment with `--with-healthy`.
2. `run` executes the red team trials. Re-running it resumes: missing and infrastructure-failed trials run again, while `agent_failed` trials are kept. A trial that failed because the model endpoint errored also counts as `agent_failed`, so move its trial directory aside before resuming.
3. `baseline` replays cited calls on one healthy deployment per environment.
4. `summary` re-judges and writes `runs/NAME/summary.md`, `summary.json`, and `traces.jsonl`.

## Verify an injection fix

Keep the fix in another worktree and put its source first on `PYTHONPATH`. Trial workers inherit the path, and each phase records its source tree, commit, and diff in `provenance.jsonl`.

```bash
PYTHONPATH=/path/to/fix/src python experiment/red_team/redteam.py recheck NAME
PYTHONPATH=/path/to/fix/src python experiment/red_team/redteam.py oracle NAME --phase after
PYTHONPATH=/path/to/fix/src python experiment/red_team/redteam.py run NAME --phase after --task-id ID ...
python experiment/red_team/redteam.py baseline NAME --phase after
python experiment/red_team/redteam.py summary NAME
```

- `recheck` re-injects each case with a confirmed `before` trace and replays its evidence. The summary reports `gone`, `still_present`, `inconclusive` (the replay timed out or returned nothing), or `error`.
- `oracle` runs the mock agent with `failure_effect` validation, to check that the fix keeps every fault working.
- `run --phase after` repeats the red team on the affected cases.

## Results

The full audit of 110 cases (75 fault types and 35 healthy controls) is stored outside git, in `~/nika-results/audit/red-team/` on the audit host. `full/report.md` holds the findings. To re-judge it without deploying labs, link it and run `summary`:

```bash
ln -s ~/nika-results/audit/red-team experiment/red_team/runs
python experiment/red_team/redteam.py summary full
```
