# Benchmark engine of release 0.1.0

The current NIKA runs release 0.1.0 by starting this branch's code as a **benchmark engine** in a container and driving its trials through engine API version 1. The engine keeps everything that defines the benchmark as release 0.1.0 had it: the network environments, failure injection and ground truth, the MCP tools the agent uses, and scoring. The current NIKA, the **frontend**, runs the agent, schedules trials, and writes the run records. The frontend's [benchmark contracts](https://github.com/sands-lab/nika/blob/dev/docs/development/benchmark-contracts.md) describe the API; this page describes what the 0.1.0 engine does for each part of it.

## Run release 0.1.0

The frontend's release manifest names the engine commit in `engine.tag`. A release run builds the image from that commit when it is missing (`nika engine build <commit>` builds it ahead of time) and starts one engine container for the run. Engine containers do not run on NIKA Remote or Firecracker.

Use the `structured` submission mode and the default access policy. Release 0.1.0 has no report mapper, tool-call limits, or diagnosis access roles, so the engine refuses a trial that asks for them.

## What each operation runs

| Operation | Release 0.1.0 code |
| --- | --- |
| `describe` | Reports API version 1, release `nika-bench` 0.1.0, the engine commit, and the hashes of `dev.yaml` and `test.yaml`. The engine serves only release 0.1.0. |
| `list_tasks` | Runs the release preflight, checks the lab images (below), and lists the split's tasks. A task ID is the `case_key` that release 0.1.0's runner gave the task, such as `dc_clos_bgp__host_crash__45fc915a`, so trial directories keep their 0.1.0 names. Size `l` tasks are `large`; all others are `light`. |
| `start_trial` | Refuses an unknown task ID or a task row, then deploys the task's environment as the 0.1.0 runner did. |
| `begin` | Injects the task's failure with the task's root causes, verifies it as 0.1.0 did, and returns the task description. |
| `agent_endpoints` | Opens the trial's MCP gateway (below). |
| `fault_check` | Returns `error`: release 0.1.0 verifies a failure once, at injection. Its verification can send probe traffic, so repeating it during the agent's turn would change what the agent observes. |
| `score` | Grades `submission.json` against `ground_truth.json` with release 0.1.0's scorer (`rule-based-v2`) and returns the scores; it writes no file. |
| `stop_trial` | Closes the gateway and undeploys the environment. It also closes a trial that another engine container started, as `nika session close` requires. |

Each trial runs in its own worker process that hosts the trial's gateway, as release 0.1.0's runner ran each trial in its own process. Kathara clients, the session event log, and the MCP servers are process-global in this code.

## What the agent sees

The gateway serves release 0.1.0's MCP servers with their tool names, schemas, and outputs unchanged. The gateway does not limit tool output; the Kathara command tools keep their own 4,000-character limit on command output.

Phase rules are those of release 0.1.0: during diagnosis the agent sees every server except `task_mcp_server`; during submission it sees only `task_mcp_server`. Release 0.1.0's harnesses advanced the phase themselves. With the engine, the frontend advances it:

1. The frontend sends the agent's final diagnosis report to the gateway's phase endpoint with the trial's phase token, which the agent never receives.
2. The gateway records the report as a `diagnosis_frozen` event in `messages.jsonl`. The first report a trial sends stays frozen.
3. The gateway switches the session to submission and returns the submission context: the report, `fault_ontology` (the fault types `list_avail_problems()` returns), and `resources` (the resources `list_resources()` returns).

The agent's harness then builds its submission prompt from that context; the prompt belongs to the frontend's harness, not to the release. The `submit` tool validates and records the answer as in release 0.1.0.

MCP requests carry the session ID in `X-Session-Id` and the trial's session token in `X-Session-Token`. The gateway records every tool call in `tool_calls.jsonl`.

## Scores

Scores of answered tasks are release 0.1.0's values. The reply follows API version 1:

| Trial | Score status | Metrics |
| --- | --- | --- |
| Answered | `scored` | Release 0.1.0's values. An answer it could not parse scores 0 rather than release 0.1.0's -1, which its leaderboard also counted as 0. |
| No answer | `no_submission` | 0 |
| No answer after an infrastructure failure the frontend reports | `infra_error` | null |
| Unreadable answer or ground truth | `grading_error` | null |

## Images and dependencies

The engine image installs the Python dependencies in `uv.lock`, resolved as of 2026-08-16, the day after the release's base commit. It runs as the frontend's user, so the environments it deploys belong to that user, and it shares the frontend's runtime state through `NIKA_STATE_DIR`.

Environments run on the host's Docker daemon, which other NIKA versions may share. Labs deploy `kathara/base`, `kathara/frr`, `kathara/p4`, and `kathara/sdn` by their `latest` tag, and the `kathara/nika-*` images that most labs deploy are built on pinned upstream digests. Before listing tasks, the engine checks these images and never retags or removes one:

- A missing upstream image is pulled by its pinned digest; a `latest` tag that names another image stops the run.
- A missing `kathara/nika-*` image is built from release 0.1.0's Dockerfile when a lab first needs it. An existing one must be the release's build step on the pinned base image; otherwise the run stops and the error says which tag to remove.
