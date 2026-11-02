# Execute network episodes from an external client

Use `nika.rollout` when a search, evaluation or training-side sampler needs one complete network episode. The API delegates to NIKA's existing benchmark runner. NIKA retains task admission, execution backends, sandbox setup, MCP access, model proxying, submission, scoring and resource cleanup.

The interface has no dependency on a particular optimizer. It accepts native NIKA task and run-configuration schemas. It does not implement other benchmark environments or a persistent reset/step worker.

Start with a working [NIKA installation](../operations/installation.md), [run configuration](../operations/configuration.md), and [external agent project](../agents/agent-reference.md#custom-agent-registration). For snapshot execution, complete the [Firecracker setup](../operations/firecracker.md) first.

## Request one episode

A version 1 `RolloutRequest` contains:

| Field | Meaning |
| --- | --- |
| `request_id` | Client-assigned identity for this attempt |
| `task` | One native task dictionary |
| `run_config` | Standard validated `RunConfig` |
| `agent_project` | Optional external agent project containing `nika-agent.yaml` |

Keep reference answers and held-out data outside the external agent project. NIKA copies that project to the normal sandbox. Set `run_config.agent.type` to the manifest's `id`; registering a project does not change the selected agent type. The rollout wrapper registers it in an output-local agent registry. See [custom agent registration](../agents/agent-reference.md#custom-agent-registration) for the manifest contract.

Run the following commands from the NIKA checkout root. [List and export a task](../operations/cli-reference.md#browse-and-try-an-example-case), using a returned task ID:

```sh
uv run nika case browse --env dc_clos --failure host_missing_ip --json
uv run nika case browse --task-id TASK_ID --output my-case.yaml
```

Replace `TASK_ID` with a task ID from the listing. Create a request from the exported row and your prepared external project:

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

The same request can be run by CLI:

```sh
NIKA_STATE_DIR=/absolute/owned/experiment-state \
  nika rollout run --request request.json --output-dir results/new-attempt
```

Use the worktree's Python environment and set `PYTHONPATH=/path/to/worktree/src` if its editable installation points at another checkout. `NIKA_STATE_DIR` must be absolute. It overrides the normal shared session/cache state root, so independent clients can avoid mixing their registries with another worktree's work.

The output directory must be new. Each retry needs a new output directory and request identity. The runner forces one task per invocation, disables internal retry passes and writes the effective configuration with the output-local result directory. The client owns retry policy and immutable policy/source attribution.

## Read terminal outcomes

`RolloutResult` separates execution outcome from reward:

| Field | Meaning |
| --- | --- |
| `request_id`, `request_sha256` | Request identity and digest of its canonical serialization |
| `outcome` | Native benchmark execution outcome |
| `reward`, `reward_name` | Native RCA F1, zero for a counted agent failure, or null for unresolved infrastructure failure |
| `score_status` | Native metric status when available |
| `session_id` | Native session identity when created |
| `returncode` | Benchmark subprocess exit code |
| `artifacts` | Native trial files, with paths relative to the output directory |

A successful execution can have reward zero. A null reward is not a scored failure. Preserve the native run, metric, model-call, tool-call and submission artifacts when computing experiment summaries.

The output also contains `request.json`, `result.json`, `run-config.yaml`, `task.yaml` and `console.log`. The request digest identifies the envelope; it is not a digest of an external project's contents. Archive that source and its dependencies separately. This API does not provide a global request-id deduplication service. Request validation, an existing output directory or failed agent registration can raise an exception before a result file is written. The CLI exits with status 1 when the terminal reward is null.

## Bind execution and budgets

Native and Firecracker episodes use the same request schema. Select the backend through the normal `nika.execution` configuration. Firecracker images, case snapshots, restore validation and disposable trial copies remain native runner responsibilities. Give an experiment its own image root when preparing or modifying caches; do not delete another client's images or trial copies.

The normal model and case timeout settings still apply. Optional agent settings add:

- `fixed_model: true`: the host proxy rejects another model name and refuses unmetered write API routes. Responses compaction counts as a generation request. Read APIs and message token counting remain available.
- `tool_call_limits`: a mapping from phase to primitive MCP-call cap, for example `diagnosis: 60` and `submission: 20`. Empty defaults preserve existing behavior.

The host enforces these limits across MCP servers and connections. A composite tool consumes one unit for each underlying MCP call. Candidate-reported counters do not control the limit. Denied calls are recorded without executing the primitive. Gateway evidence includes actual arguments, raw output, duration and call identity. Controller-selected roles and primitive limits are forwarded to the remote Firecracker gateway.

The model proxy records actual request and response bodies, response status, duration and usage. Model names identify the serving model; they do not establish a weight-checkpoint digest. Fixed-model mode does not by itself pin every provider sampling parameter. A comparative experiment must also bind and audit its inference configuration.

## Cancel and retry safely

The wrapper runs a benchmark subprocess in its process group. A supervisor must propagate cancellation to the whole group so the benchmark lifecycle can close its session and sandbox. Do not kill only an arbitrary child or delete its result directory while a trial is active. Inspect recorded session identities and the configured Firecracker trial root after an ungraceful host failure.

This API does not restore an interrupted model trajectory. Start a new attempt after cleanup, preserving the old attempt's logs. Higher-level experiment resume, source freezing and candidate selection belong to the client.

## Training-side integration limits

Whole-episode requests and terminal rewards can support an external sampler. The client must still supply policy/checkpoint identity, trainer transport, concurrency and backpressure, behavior-policy metadata and log probabilities when required, and any intermediate reward or reset/step protocol. NIKA's MCP interface remains the current within-episode interaction boundary.

Task/agent separation follows the integration boundaries used by [Harbor](https://docs.harborframework.com/tasks/overview) and [Terminal-Bench](https://www.tbench.ai/). Separating externally produced predictions from native evaluation follows [SWE-bench](https://www.swebench.com/SWE-bench/guides/evaluation/). This API preserves NIKA's network-specific task and scoring semantics.
