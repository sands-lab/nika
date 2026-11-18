# Benchmark audit

Use the benchmark audit to check that a task starts healthy, exposes its declared fault, and keeps that fault observable. The audit runs real NIKA environments and records evidence for release review.

## Run the audit

Run from a clean, committed checkout with Docker and the environment images available:

```shell
uv run python -m scripts.benchmark_audit.matrix --scenario campus_lan --fault dhcp_missing_subnet
```

The matrix reads the dev and test catalogs of benchmark release 0.2.0 and checks them against the current implementation. It does not change published tasks or scores. Omit the scenario and fault filters to audit the full catalog.

The default is two workers. Host admission checks available memory and load, and Containerlab environments share a serial slot. Other classes retain their declared concurrency limits. Protocol convergence and persistence checks can make a task take several minutes. Each audit closes the session it created when it finishes or raises an exception.

| Option | Effect |
| --- | --- |
| `--scenario NAME`, `--fault NAME` | Select tasks to audit. |
| `--repeats N` | Run each selected task on N fresh deployments. Every repeat must pass. |
| `--force` | Replace the selected tasks' stored measurements. |
| `--retry-failed` | Rerun selected tasks whose current result did not pass. |
| `--allow-dirty` | Collect diagnostic measurements while editing. These results do not count toward admission. |

## Review the evidence

Read the JSON records in `runtime/environment-audit-results/`. `NIKA_AUDIT_RESULTS_DIR` selects another output directory. Each record contains the task parameters, stage results, observations, Git commit, working-tree state, timestamps, session ID, and container image identities.

For a fault task, the audit checks:

1. The environment and target path are healthy before injection.
2. The symptom predicate stays negative on the healthy environment, and the default agent tools show no stable anomaly.
3. The standard NIKA injection workflow verifies the fault, aligns saved configurations, and writes the injection record and ground truth.
4. A scan compares agent-readable files and network state before and after injection.
5. The declared symptom appears while an independent control path stays healthy, where the topology provides one.
6. The reference Oracle diagnoses the injected task through the session's MCP tools and records a solved result in `oracle.json`.
7. The fault artifact and symptom persist through two further observation windows.

Healthy tasks run the environment, agent-view, and Oracle checks without injection. The Oracle runs on the same deployed lab; its evidence and failed steps remain in the session artifacts. A missing or unsolved Oracle result prevents admission. Only `pass` admits a task. Missing, unsupported, skipped, and failed checks stay distinct. When no independent control path exists, the audit records that limitation and relies on the baseline, negative control, and repeated symptom checks.

A stored result counts only for the current clean commit, with completed provenance and the required agent-view and Oracle stages. Old or incompatible records require another run. The matrix reports how many selected tasks have current passing evidence; historical pass counts do not establish current admission.

### Injection traces

The leak scan checks files, processes, sockets, links, addresses, routes, firewall rules, queueing state, and supported device tables. It flags new fault-revealing names or content, backup files, and copies of pre-fault state. It masks volatile values in text before comparison and lists ordinary process changes for review.

File times are separate evidence. `file_time_asymmetries` records mtime/ctime changes relative to unchanged peers with the same declared role, platform capabilities, and service type. `file_time_coverage` and `file_time_unreadable_nodes` show where metadata was available. Time differences are review leads, not automatic failures: native protocol and management logs can change as legitimate evidence.

The scan does not establish that an agent exploited a trace. Use the [submission audit skill](../../.agents/skills/nika-submission-audit/SKILL.md) to review recorded agent behavior.
