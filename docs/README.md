# NIKA documentation

Use this index to find the shortest path for your task. The root [`README.md`](../README.md) introduces NIKA and provides the first-run workflow.

## Run and operate NIKA

| Goal | Document |
| --- | --- |
| Configure agents, labs, MCP, and benchmark runs | [Run configuration reference](configuration.md) |
| Choose and deploy a lab | [Network scenario reference](network-scenarios.md) |
| Load the Cisco XRd image for `iosxr_simple_bgp` | [Cisco IOS-XR (XRd) image setup](iosxr-xrd-setup.md) |
| Build the RouterOS image for `routeros_simple_bgp` | [MikroTik RouterOS (vrnetlab) image setup](mikrotik-routeros-setup.md) |
| Select and inject a fault | [Failure reference](failures.md) |
| Use every CLI command and option | [CLI reference](cli-reference.md) |
| Run labs on another host | [Remote lab execution](remote.md) |
| Run agents in an isolated microVM | [Agent sandbox execution](agent-sandbox.md) |

## Extend NIKA

| Goal | Document |
| --- | --- |
| Add a scenario, failure, traffic source, or benchmark case | [Create benchmark tasks](creating-benchmark-tasks.md) |
| Implement and register an agent | [Custom agent integration](custom-agents.md) |
| Attach troubleshooting skills to an agent | [Agent skills](agent-skills.md) |
| Understand the built-in agent implementations | [Agent implementation reference](agent-implementations.md) |
| Configure a community agent | [Community agent references](agents/community/README.md) |

## Evaluate and contribute

| Goal | Document |
| --- | --- |
| Understand the checked-in benchmark matrices | [Benchmark configuration reference](benchmark-configuration.md) |
| Implement or evaluate root-cause labels | [Root-cause ground truth and scoring](root-cause-evaluation.md) |
| Package a leaderboard result | [Leaderboard submission](leaderboard-submission.md) |
| Run the test suites | [Testing guide](testing.md) |

The code registries are the source of truth for available scenarios and failures. Run `uv run nika env list` and `uv run nika failure list` to inspect the installed checkout.

## Map documentation to implementation

Use these links when a reference page does not expose enough implementation detail for a change or review.

| Topic | Primary implementation files |
| --- | --- |
| CLI commands | [`main.py`](../src/nika/cli/main.py), [`commands/`](../src/nika/cli/commands/) |
| Run configuration | [`schema.py`](../src/nika/run_config/schema.py), [`loader.py`](../src/nika/run_config/loader.py), [`nika.example.yaml`](../config/nika.example.yaml) |
| Scenario registry and contracts | [`net_env_pool.py`](../src/nika/net_env/net_env_pool.py), [`base.py`](../src/nika/net_env/base.py) |
| SNDlib topology import | [`topology/sndlib/`](../src/nika/topology/sndlib/), [`net_env/isp/`](../src/nika/net_env/isp/) |
| Failure registry and contracts | [`prob_pool.py`](../src/nika/problems/prob_pool.py), [`problem_base.py`](../src/nika/problems/problem_base.py), [`root_cause.py`](../src/nika/problems/root_cause.py) |
| Failure implementations | [`problems/`](../src/nika/problems/) |
| Agent protocol and registry | [`protocols.py`](../src/agent/protocols.py), [`registry.py`](../src/agent/registry.py) |
| Built-in agent implementations | [`byo/`](../src/agent/byo/), [`cli/`](../src/agent/cli/), [`sdk/`](../src/agent/sdk/) |
| Agent sandbox | [`runner.py`](../src/agent/sandbox/runner.py), [`manifest.py`](../src/agent/sandbox/manifest.py) |
| Shared agent skills | [`utils/skills.py`](../src/agent/utils/skills.py), [`skills/`](../src/agent/skills/skills/) |
| Community agents | [`community/`](../src/agent/community/), [community agent references](agents/community/README.md) |
| Benchmark generation and execution | [`generate_benchmark.py`](../benchmark/generate_benchmark.py), [`workflows/benchmark/`](../src/nika/workflows/benchmark/) |
| Leaderboard packaging | [`workflows/leaderboard/`](../src/nika/workflows/leaderboard/) |
| Remote lab control | [`remote/server.py`](../src/nika/remote/server.py), [`remote/workflows.py`](../src/nika/remote/workflows.py) |
| Test suites | [`tests/`](../tests/) |
