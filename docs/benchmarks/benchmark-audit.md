# Benchmark audit

Maintainers run the audit before they publish or change a task. It checks that the task starts healthy, that its failure is present, and that the failure stays observable to the agent. The audit deploys real NIKA environments in Docker and records the evidence for release review.

## Audit tasks

Run the audit from a clean, committed checkout, with Docker and the environment images available:

```shell
uv run python -m scripts.benchmark_audit.matrix --scenario campus_lan --fault dhcp_missing_subnet
```

The audit reads the dev and test tasks of benchmark release 0.2.0 and checks them against the current code. It does not change published tasks or scores. Leave out `--scenario` and `--fault` to audit every task.

By default the audit runs two tasks at a time. Containerlab environments run one at a time, and each admission waits until the host has enough memory and load. A task can take several minutes, because some checks wait for protocols to converge and for a failure to persist. Each audit closes the session it created, even when it raises an error.

## Read the results

A task is admitted only when every check passes. Missing, unsupported, skipped, and failed checks are reported separately. Each result is a JSON record in `runtime/environment-audit-results/`. Set `NIKA_AUDIT_RESULTS_DIR` to write them somewhere else. A record holds the task parameters, the check results and observations, the Git commit and working-tree state, timestamps, the session ID, and the container image identities.

A stored result counts only for the current clean commit, and only when it has every required check. Older or incompatible records need a new run. The matrix reports how many selected tasks have current passing results. Past pass counts do not establish current admission.

---

The rest of this page is reference material: the command options, what the audit checks, and how the leak scan works.

## Options

| Option | Effect |
| --- | --- |
| `--scenario NAME`, `--fault NAME` | Select the tasks to audit. |
| `--repeats N` | Run each selected task on N fresh deployments. Every repeat must pass. |
| `--jobs N` | Number of tasks to run at once. Defaults to 2. |
| `--resource-class` | Select only `light`, `large`, `k8s`, or `clab` tasks. |
| `--force` | Rerun the selected tasks and replace their stored results. |
| `--retry-failed` | Rerun the selected tasks whose current result did not pass. |
| `--allow-dirty` | Run on uncommitted edits to collect diagnostics. Such records never count toward a release report. |

## What the audit checks

For a fault task, the audit checks these things in order:

1. The environment and the target path are healthy before injection.
2. The symptom check is negative on the healthy environment, and the default agent tools show no stable anomaly.
3. The standard injection workflow verifies the failure, aligns the saved configurations, and writes the injection record and the ground truth.
4. A scan compares the files and network state that the agent can read before and after injection.
5. The declared symptom appears while an independent control path stays healthy, where the topology has one.
6. The reference [oracle solver](oracle-solver.md) diagnoses the task through the session's MCP tools and records a solved result in `oracle.json`.
7. The failure and its symptom persist through two more observation windows.

Healthy tasks run the environment, agent-view, and oracle checks without injecting anything. The oracle runs on the same deployed lab, and its evidence stays in the session artifacts. A missing or unsolved oracle result blocks admission.

When the topology has no independent control path, the audit records that limitation. It then relies on the baseline, the negative control, and the repeated symptom checks.

## Injection traces

The leak scan checks files, processes, sockets, links, addresses, routes, firewall rules, queueing state, and supported device tables. It flags new names or content that reveal the failure, backup files, and copies of the pre-fault state. Before comparison, it masks volatile values in text, and it lists ordinary process changes for review.

File times are separate evidence. `file_time_asymmetries` lists changes to modification and change times that differ from unchanged peers with the same role and service. `file_time_coverage` and `file_time_unreadable_nodes` show where metadata was available. A time difference is a lead for review, not an automatic failure, because native protocol and management logs can change as legitimate evidence.

The scan does not show that an agent used a trace. To review recorded agent behavior, use the [submission audit skill](../../.agents/skills/nika-submission-audit/SKILL.md).
