# NIKA documentation

Start with the root [README](../README.md) to install NIKA and run one incident. Then choose the page that matches your task.

## Start and operate a lab

| Goal | Read | Type |
| --- | --- | --- |
| Configure providers, labs, MCP, sandboxing, and result paths | [Run configuration](configuration.md) | Reference |
| Find a command, option, session rule, or artifact location | [CLI reference](cli-reference.md) | Reference |
| Select a scenario and its backend, scale, workload, or prerequisites | [Network scenarios](network-scenarios.md) | Reference |
| Select, inspect, and inject a failure | [Failure taxonomy and reference](failures.md) | Reference |
| Run the lab host and agent host separately | [Remote lab execution](remote.md) | How-to |
| Run a supported agent in a Docker Sandbox microVM | [Docker Sandbox execution](agent-sandbox.md) | How-to |

## Choose or integrate an agent

| Goal | Read | Type |
| --- | --- | --- |
| Compare registered agents, providers, and execution modes | [Agent implementation reference](agent-implementations.md) | Reference |
| Implement and register a `TroubleshootingAgent` | [Custom agent integration](custom-agents.md) | How-to |
| Add reusable instructions for Claude or Codex agents | [Configure agent skills](agent-skills.md) | How-to |
| Configure a community-maintained agent | [Community agent references](agents/community/README.md) | Reference |

## Run, evaluate, and publish a benchmark

| Goal | Read | Type |
| --- | --- | --- |
| Run a frozen release or an ad-hoc case matrix, or inspect generated coverage | [Benchmark configuration](benchmark-configuration.md) | Reference |
| Understand ground truth, agent submissions, and scores | [Root-cause ground truth and scoring](root-cause-evaluation.md) | Reference |
| Package a completed official release run | [Leaderboard submission](leaderboard-submission.md) | How-to |

## Develop scenarios, failures, and verifiers

| Goal | Read | Type |
| --- | --- | --- |
| Add a scenario, failure, traffic source, or working-matrix case | [Create benchmark tasks](creating-benchmark-tasks.md) | How-to |
| Define healthy-network intents or implement a verifier | [Network validation contracts](validation-contracts.md) | Reference |
| Select and run the smallest relevant test suite | [Testing](testing.md) | Guide |

## Maintain the documentation

Read the code registries before changing a catalog or count:

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
