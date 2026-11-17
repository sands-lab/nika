# NOOA agent example

[`src/agent/custom/nooa.py`](../../src/agent/custom/nooa.py) is a complete custom agent built on [NVIDIA-labs Object Oriented Agents](https://github.com/NVIDIA-NeMo/labs-OO-Agents) (NOOA). It runs as agent type `custom.nooa`. Copy it as a starting point for an agent on any framework.

NOOA runs model-generated Python. Like every agent, it runs in its own sandbox, which reaches only the environment's MCP tools and the model endpoint.

## What the complete agent adds

The [short version](custom-agents.md#1-write-your-agent) only diagnoses. The complete agent adds three things:

| Addition | What it does |
| --- | --- |
| Tool inventory | Lists the MCP tools before the first model turn, so the model starts knowing which tools exist instead of spending turns to find out. |
| Trace events | Writes tool calls and model turns to `messages.jsonl`, so the session trace shows each step. See the [trace event contract](agent-reference.md#trace-event-contract). |
| Step limit | Applies the run's [step limit](agent-reference.md#step-limit). When the limit is reached, the agent reports its latest model text, as the built-in agents do. |

## Run it

NIKA installs the agent's `dependencies` in its sandbox on every run. The example pins LiteLLM below 1.106 because version 1.106.0rc1 fails against NOOA's `openai` client.

Copy your active configuration so the NOOA profile keeps your sandbox runtime, then select an OpenAI-compatible endpoint, such as a local vLLM server:

```shell
cp config/nika.yaml config/nooa.yaml
uv run nika config set --run-config config/nooa.yaml \
  agent.type=custom.nooa agent.provider=custom \
  agent.model=nvidia/Qwen3.6-35B-A3B-NVFP4 \
  agent.base_url=http://localhost:8000/v1
uv run nika agent run --run-config config/nooa.yaml --problem dc_clos_s_link_down
```
