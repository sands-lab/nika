# NIKA documentation

Start with the root [README](../README.md) to install NIKA and run one incident. Then choose the page that matches your task.

## Start and operate a lab

| Goal | Read | Type |
| --- | --- | --- |
| Configure providers, labs, MCP, sandboxing, and result paths | [Run configuration](operations/configuration.md) | Reference |
| Find a command, option, session rule, or artifact location | [CLI reference](operations/cli-reference.md) | Reference |
| Select a scenario, backend, scale, or prerequisites | [Network scenarios](operations/network-scenarios.md) | Reference |
| Select, inspect, and inject a failure | [Failure taxonomy and reference](operations/failures.md) | Reference |
| Run the lab host and agent host separately | [Remote lab execution](operations/remote.md) | How-to |
| Run a supported agent in a Docker Sandbox microVM | [Docker Sandbox execution](operations/agent-sandbox.md) | How-to |

## Choose or integrate an agent

| Goal | Read | Type |
| --- | --- | --- |
| Compare registered agents, providers, and execution modes | [Agent implementation reference](agents/agent-implementations.md) | Reference |
| Implement and register a `TroubleshootingAgent` | [Custom agent integration](agents/custom-agents.md) | How-to |
| See which MCP servers a session mounts and how to use packet capture | [MCP servers](agents/mcp-servers.md) | Reference |
| Add reusable instructions for Claude or Codex agents | [Configure agent skills](agents/agent-skills.md) | How-to |
| Configure a community-maintained agent | [Community agent references](agents/community/README.md) | Reference |

## Run, evaluate, and publish a benchmark

| Goal | Read | Type |
| --- | --- | --- |
| Run a frozen release or an ad-hoc case matrix, or inspect generated coverage | [Benchmark configuration](benchmarks/benchmark-configuration.md) | Reference |
| Understand ground truth, agent submissions, and scores | [Root-cause ground truth and scoring](benchmarks/root-cause-evaluation.md) | Reference |
| Package a completed official release run | [Leaderboard submission](benchmarks/leaderboard-submission.md) | How-to |

## Develop scenarios, failures, and verifiers

| Goal | Read | Type |
| --- | --- | --- |
| Add a scenario, failure, traffic source, or candidate case | [Create benchmark tasks](development/creating-benchmark-tasks.md) | How-to |
| Define healthy-network intents or implement a verifier | [Scenario validation](development/scenario-validation.md) | Reference |
| Select and run the smallest relevant test suite | [Testing](development/testing.md) | Guide |

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
| Scenario validation | [`contract.py`](../src/nika/net_env/contract.py), [`isp/contract.py`](../src/nika/net_env/isp/contract.py) |
| Failure registry and labels | [`registry.py`](../src/nika/problems/registry.py), [`base.py`](../src/nika/problems/base.py), [`rca/`](../src/nika/problems/rca/) |
| Agent protocol and registry | [`protocols.py`](../src/agent/protocols.py), [`registry.py`](../src/agent/registry.py) |
| MCP server catalog and selection | [`mcp/registry.py`](../src/nika/mcp/registry.py), [`mcp/servers/`](../src/nika/mcp/servers/), [`mcp/gateway/`](../src/nika/mcp/gateway/) |
| Benchmark generation | [`workflows/benchmark/`](../src/nika/workflows/benchmark/) (`nika benchmark generate` / `select`) |
| Test suites | [`tests/`](../tests/) |
