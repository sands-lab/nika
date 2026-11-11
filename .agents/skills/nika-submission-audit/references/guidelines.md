# Audit guidelines

The audit method. What earlier audits learned is in [the lessons](lessons.md).

## Issue classes

| Class | What it can look like in trajectories | First check |
| --- | --- | --- |
| `shortcut` | The agent names the fault right after reading a file, rule, process, or log whose *name* states it; correct answers with very few calls; reasoning about "fault injection". | Find the first step where the answer appears and the output just before it. Would an operator see that string on a real network? |
| `harness_privilege` | Tools in the session init that reach outside the network under test: web, connectors, subagents, skills. | The init step of the first trial; compare across trials (fingerprint drift). |
| `out_of_scope` | Commands that read `/hostlab`, `/opt/nika`, `run.json`, the sandbox manifest, or walk above the workspace. | Did the read succeed, and did the output carry task information? |
| `fault_not_realized` | Every configuration fails a task; agents report healthy; tool outputs show no symptom at all. | Is the triggering traffic present in the scenario workload? |
| `signal_invisible` | The symptom exists but outputs are cut, sampled, or come from the wrong vantage point; the tool that would show it is missing. | Look for truncation warnings, `capped` flags, `command not found`. |
| `label_unidentifiable` | Agents describe the right mechanism and pick a neighbouring label; one label pair is confused across models. | Read both label descriptions against the observable end state. |
| `healthy_lure` | Healthy tasks called faulty by most models, all pointing at the same by-design behaviour; or at a real defect nobody injected. | Scenario brief and `probe_intent`; then confirm live. |
| `grading_error` | Re-grade differs from the submitted score; a scoring rule rewards spraying labels or empty answers. | `audit.py run`: `scores` and `regrade` hits. |
| `harness_defect` | Tracebacks, session errors, empty reports, final turns cut at the token limit, a reasoning parser swallowing the answer. | Group error texts across trials; one cause usually explains many. |
| `agent_integrity` | State-changing commands; the fault gone at cleanup. | Was the faulted component touched before the report? |
| `run_validity` | Trials on different commits, budgets, or tool sets. | Fingerprint drift. |

## Evidence

* Cite the step index from the packet and copy a short excerpt character for character. Prefer the most specific line over a summary.
* A finding about the benchmark needs at least two trials, or one trial plus a live confirmation. A finding about one leaderboard submission (an edited score, a forbidden tool call) needs one trial.
* Separate what the agent saw from what it concluded. A leak is about what it saw.

## Judging

* By-design behaviour the task description does not state is a lure, not a fault. A reading that holds across all models and healthy trials of an environment is a benchmark property, not an agent mistake.
* A log line, file, or name is a leak only if it is specific to the faulted device and absent from comparable devices; the fault mechanism itself is not a leak.
* Do not refute a finding without an observation that holds.
* If a signature fires on most trials of every leaderboard submission, narrow it.
