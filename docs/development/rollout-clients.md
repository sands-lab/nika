# Run network episodes from an external client

Use `nika.rollout` when your own search, evaluation, or training code needs one complete network episode. NIKA runs the episode through its benchmark runner. It admits the task, starts the execution backend, sets up the agent sandbox, serves the MCP tools, proxies the model, collects the submission, scores it, and cleans up the resources. Your client supplies the task and the agent configuration, and gets back the outcome, the reward, and the artifact paths.

The API does not depend on a particular optimizer. It accepts NIKA's native task and run-configuration schemas. It does not provide other benchmark environments or a persistent reset/step worker.

Before you start, complete a working [NIKA installation](../operations/installation.md), set up your [run configuration](../operations/configuration.md), and prepare an [external agent project](../agents/agent-reference.md#custom-agent-registration). For snapshot execution, complete the [Firecracker setup](../operations/firecracker.md) first.

Terms follow the [glossary](../../GLOSSARY.md). A **rollout** is one complete task attempt.

## Run one episode

1. Export a task from the catalog. Run these commands from the NIKA checkout root. Use a task ID from the listing. See [browse and try an example case](../operations/cli-reference.md#browse-and-try-an-example-case).

   ```sh
   uv run nika case browse --env dc_clos --failure host_missing_ip --json
   uv run nika case browse --task-id TASK_ID --output my-case.yaml
   ```

2. Build a request from the exported task and your external agent project, then run it:

   ```python
   from pathlib import Path

   import yaml
   from nika.rollout import RolloutRequest, run_rollout
   from nika.run_config.schema import RunConfig

   config = RunConfig.model_validate(yaml.safe_load(Path("config/nika.yaml").read_text()))
   project = Path("/absolute/path/to/agent-project")
   manifest = yaml.safe_load((project / "nika-agent.yaml").read_text())
   config.agent.type = manifest["id"]
   task = yaml.safe_load(Path("my-case.yaml").read_text())["cases"][0]
   request = RolloutRequest(
       request_id="candidate-task-42-attempt-1",
       task=task,
       run_config=config,
       agent_project=project,
   )
   Path("request.json").write_text(request.model_dump_json(indent=2))
   result = run_rollout(request, Path("results/candidate-task-42-attempt-1"))
   print(result.model_dump_json(indent=2))
   ```

3. To run a saved request from the command line instead:

   ```sh
   NIKA_STATE_DIR=/absolute/owned/experiment-state \
     nika rollout run --request request.json --output-dir results/new-attempt
   ```

A request has these fields:

| Field | Meaning |
| --- | --- |
| `request_id` | Client-assigned identity for this attempt |
| `task` | One native task dictionary |
| `run_config` | Standard validated `RunConfig` |
| `agent_project` | Optional external agent project containing `nika-agent.yaml` |

Keep reference answers and held-out data outside the external agent project. NIKA copies the project into the normal sandbox. Set `run_config.agent.type` to the manifest's `id`. Registering a project does not change the selected agent type. The rollout wrapper registers the project in an agent registry inside the output directory. See [custom agent registration](../agents/agent-reference.md#custom-agent-registration) for the manifest contract.

Each attempt needs a new output directory and a new `request_id`. The output directory must not exist yet. The runner runs one task per invocation, turns off its internal retry passes, and writes the effective configuration with the output directory as its result directory. Your client owns the retry policy and records which policy and source produced each request.

## Read the result

`RolloutResult` separates the execution outcome from the reward:

| Field | Meaning |
| --- | --- |
| `request_id`, `request_sha256` | Request identity and digest of its canonical serialization |
| `outcome` | Native benchmark execution outcome |
| `reward`, `reward_name` | Native RCA F1 (`rca_f1`), zero for a counted agent failure, or null for an unresolved infrastructure failure |
| `score_status` | Native metric status, when available |
| `session_id` | Native session identity, when a session was created |
| `returncode` | Exit code of the benchmark subprocess |
| `artifacts` | Native trial files, with paths relative to the output directory |

A successful run can have a reward of zero. A null reward is not a scored failure. When you compute experiment summaries, keep the native run, metric, model-call, tool-call, and submission artifacts.

The output directory also contains `request.json`, `result.json`, `run-config.yaml`, `task.yaml`, and `console.log`. The request digest identifies the request envelope. It does not cover the contents of the external agent project, so archive that project and its dependencies separately. The API does not deduplicate request IDs. A request that fails validation, an existing output directory, or a failed agent registration raises an exception before `result.json` is written. The CLI exits with status 1 when the terminal reward is null.

## Limit model and tool use

Native and Firecracker episodes use the same request schema. Choose the backend through the normal `nika.execution` configuration. The normal model and case timeout settings still apply. Two optional agent settings add limits:

- `fixed_model: true`: the host proxy rejects any other model name and refuses write API routes that it cannot meter. Responses compaction counts as a generation request. Read APIs and message token counting stay available.
- `tool_call_limits`: a mapping from phase to primitive MCP-call cap, such as `diagnosis: 60` and `submission: 20`. The empty default keeps the existing behavior.

The host enforces these limits across all MCP servers and connections. A composite tool uses one unit for each underlying MCP call. The candidate's own counters do not control the limit. A denied call is recorded and not executed. The gateway evidence records the actual arguments, raw output, duration, and call identity. The controller forwards the selected roles and primitive limits to the remote Firecracker gateway.

The model proxy records the actual request and response bodies, the response status, the duration, and the usage. A model name identifies the serving model; it does not establish a weight-checkpoint digest. Fixed-model mode does not pin every provider sampling parameter by itself. A comparative experiment must also record and audit its inference configuration.

## Cancel and retry safely

The wrapper runs the benchmark subprocess in its own process group. Your supervisor must send cancellation to the whole group, so the benchmark lifecycle can close the session and the sandbox. Do not kill only one child process, and do not delete a result directory while a trial is active. After an ungraceful host failure, check the recorded session identities and the configured Firecracker trial root.

The API does not restore an interrupted model trajectory. After cleanup, start a new attempt with a new output directory and request ID, and keep the logs of the old attempt. Experiment-level resume, source freezing, and candidate selection belong to your client.

---

## Reference: state, output, and integration limits

**State directory.** Set `NIKA_STATE_DIR` to an absolute path when independent clients must not share session and cache state. It overrides the shared state root that normal sessions and caches use. If your editable installation points at another checkout, also set `PYTHONPATH=/path/to/worktree/src` and use that worktree's Python environment.

**Images and caches.** Firecracker images, case snapshots, restore validation, and disposable trial copies are managed by the native runner. Give an experiment its own image root when you prepare or change caches. Do not delete another client's images or trial copies.

**Training-side integration.** Whole-episode requests and terminal rewards can support an external sampler. The client must still provide policy and checkpoint identity, the trainer transport, concurrency and backpressure control, behavior-policy metadata and log probabilities when they are required, and any intermediate reward or reset/step protocol. NIKA's MCP interface remains the interaction boundary within an episode.

The task and agent separation follows the integration boundaries used by [Harbor](https://docs.harborframework.com/tasks/overview) and [Terminal-Bench](https://www.tbench.ai/). Separating externally produced predictions from native evaluation follows [SWE-bench](https://www.swebench.com/SWE-bench/guides/evaluation/). This API keeps NIKA's network-specific task and scoring semantics.
