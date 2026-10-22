# NOOA agent example

[`src/agent/custom/nooa.py`](../../src/agent/custom/nooa.py) is the complete agent from [Integrate a custom agent](custom-agents.md), built on [NVIDIA-labs Object Oriented Agents](https://github.com/NVIDIA-NeMo/labs-OO-Agents) (NOOA). It runs as agent type `custom.nooa`. Copy it as a starting point for an agent on any framework.

NOOA runs model-generated Python. Like every agent, it runs in its own sandbox, which reaches only the lab's MCP tools and the model endpoint.

## What the complete agent adds

Compared with the [short version](custom-agents.md#1-write-your-agent), the agent adds:

| Addition | Why |
| --- | --- |
| `tools = {...}` before `...` | NOOA runs this code before the first model turn, so the model starts with the tool inventory instead of spending turns discovering it. |
| `_trace_tool_calls`, `_trace_llm_turns` | Write [trace events](agent-reference.md#trace-event-contract) to `messages.jsonl`, so the session trace shows each model turn and tool call. NIKA counts steps and tool calls from its own records either way. |
| `max_iterations=self.max_steps` and `GenerationError` | Apply NIKA's [step limit](agent-reference.md#step-limit) and, when it is reached, report the latest model text like NIKA's other agents. |

## Run it

NIKA installs the agent's `dependencies` in its sandbox on every run. They pin LiteLLM because `1.106.0rc1` fails against NOOA's `openai` client.

Copy your active configuration so the NOOA profile keeps your sandbox runtime, then select an OpenAI-compatible endpoint, such as a local vLLM server:

```shell
cp config/nika.yaml config/nooa.yaml
uv run nika config set --run-config config/nooa.yaml \
  agent.type=custom.nooa agent.provider=custom \
  agent.model=nvidia/Qwen3.6-35B-A3B-NVFP4 \
  agent.base_url=http://localhost:8000/v1
uv run nika agent run --run-config config/nooa.yaml --problem dc_clos_s_link_down
```
