# NIKA documentation

Start with the root [README](../README.md) to install NIKA and run one incident. Use one canonical page for each topic; related pages link to it instead of repeating its instructions.

## Operate NIKA

| Task | Page | Content type |
| --- | --- | --- |
| Configure agents, labs, MCP, sandboxes, and benchmark defaults | [Run configuration](configuration.md) | Reference |
| Find a command, option, session rule, or artifact path | [CLI](cli-reference.md) | Reference |
| Choose and configure a lab | [Network scenarios](network-scenarios.md) | Reference |
| Select, inspect, and inject a fault | [Failures](failures.md) | Reference |
| Run a lab on another host | [Remote lab execution](remote.md) | How-to |
| Run an agent in a microVM | [Docker Sandbox execution](agent-sandbox.md) | How-to |

## Work with agents

| Task | Page | Content type |
| --- | --- | --- |
| Compare built-in agents and their provider support | [Agent implementations](agent-implementations.md) | Reference |
| Implement and register an agent | [Custom agent integration](custom-agents.md) | How-to |
| Attach troubleshooting instructions to Claude or Codex agents | [Agent skills](agent-skills.md) | How-to |
| Configure a community agent | [Community agents](agents/community/README.md) | Reference |

## Extend and evaluate NIKA

| Task | Page | Content type |
| --- | --- | --- |
| Add a scenario, failure, traffic source, or benchmark case | [Create benchmark tasks](creating-benchmark-tasks.md) | How-to |
| Define scenario expectations or implement a verifier | [Network validation contracts](validation-contracts.md) | Reference |
| Inspect working matrices and frozen releases | [Benchmark configuration](benchmark-configuration.md) | Reference |
| Define labels or understand scoring | [Root-cause ground truth and scoring](root-cause-evaluation.md) | Reference |
| Package and submit an official release run | [Leaderboard submission](leaderboard-submission.md) | How-to |
| Select and run test suites | [Testing](testing.md) | Guide |

## Source of truth

The code registries define installed scenarios, failures, and agents. Inspect the current checkout before changing a catalog:

```shell
uv run nika env list
uv run nika failure list
uv run nika agent list
```

| Contract | Implementation |
| --- | --- |
| Run configuration | [`schema.py`](../src/nika/run_config/schema.py), [`loader.py`](../src/nika/run_config/loader.py), [`nika.example.yaml`](../config/nika.example.yaml) |
| CLI and workflows | [`main.py`](../src/nika/cli/main.py), [`commands/`](../src/nika/cli/commands/), [`workflows/`](../src/nika/workflows/) |
| Scenario registry | [`net_env_pool.py`](../src/nika/net_env/net_env_pool.py), [`base.py`](../src/nika/net_env/base.py) |
| Network validation contracts | [`contract.py`](../src/nika/net_env/contract.py), [`isp/contract.py`](../src/nika/net_env/isp/contract.py) |
| Failure registry and labels | [`prob_pool.py`](../src/nika/problems/prob_pool.py), [`problem_base.py`](../src/nika/problems/problem_base.py), [`root_cause.py`](../src/nika/problems/root_cause.py) |
| Agent protocol and registry | [`protocols.py`](../src/agent/protocols.py), [`registry.py`](../src/agent/registry.py) |
| Benchmark generation | [`generate_benchmark.py`](../benchmark/generate_benchmark.py), [`workflows/benchmark/`](../src/nika/workflows/benchmark/) |
| Test suites | [`tests/`](../tests/) |
