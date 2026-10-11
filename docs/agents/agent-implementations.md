# Choose and run an agent

NIKA runs troubleshooting agents against network incidents and scores their diagnoses. To get started, use `byo.langgraph`; you can change its provider or model without writing a new agent.

## Run your first incident

Complete [Installation](../operations/installation.md), run `uv sync`, and add your provider key to the repository's `.env`:

```dotenv
OPENAI_API_KEY=<your-api-key>
```

```shell
uv run nika agent run -a byo.langgraph -p openai -m gpt-5-mini -n 20 \
  --problem dc_clos_s_link_down
```

NIKA creates the lab, injects the failure, runs the agent, evaluates its answer, and closes the lab. `-n 20` allows up to 20 model responses per phase. Keep the printed session ID.

Prefer an interactive setup? Run `uv run nika` to [choose a task and agent](../operations/tui.md).

## Choose another agent

| If you want to… | Use |
| --- | --- |
| Run a model through a host-based tool loop | `byo.langgraph` |
| Use a specific orchestration framework | `byo.mcp_agent` or `byo.autogen` |
| Evaluate Codex or Claude Code's own agent loop | `cli.codex` or `cli.claude` |
| Use the vendor's Python agent SDK | `sdk.codex_sdk` or `sdk.claude_sdk` |
| Use SADE's troubleshooting skills | [`community.sade`](community/sade.md) |
| Check the pipeline without an LLM | `mock` |

BYO agents run on the host. CLI, SDK, and SADE agents require [Docker Sandbox setup](../operations/agent-sandbox.md#prerequisites). For example:

```shell
uv run nika agent run -a cli.codex -p openai -m gpt-5-mini -n 20 \
  --problem dc_clos_s_link_down
```

SDK agents also need `uv sync --extra sdk --prerelease=allow`. See the [agent reference](agent-reference.md) for supported providers and options, or [custom agents](custom-agents.md) to add your own investigation loop.

## Use a self-hosted model

Use `byo.langgraph` with `-p custom` and an OpenAI-compatible endpoint. The model must support tool calling.

### Ollama

Install [Ollama](https://docs.ollama.com/), start `ollama serve`, then run in another terminal:

```shell
ollama pull qwen2.5:7b
uv run nika agent run -a byo.langgraph -p custom -m qwen2.5:7b -n 20 \
  --base-url http://localhost:11434/v1 --problem dc_clos_s_link_down
```

### vLLM

Install [vLLM](https://docs.vllm.ai/en/stable/) and start a tool-enabled server:

```shell
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

In another terminal:

```shell
uv run nika agent run -a byo.langgraph -p custom \
  -m Qwen/Qwen2.5-7B-Instruct -n 20 \
  --base-url http://localhost:8000/v1 --problem dc_clos_s_link_down
```

Other models may need different [vLLM tool-calling settings](https://docs.vllm.ai/en/stable/features/tool_calling/). For authenticated endpoints, set `NIKA_CUSTOM_API_KEY` in `.env`; local unauthenticated endpoints need no key.

To save these flags, use `agent.type`, `agent.provider`, `agent.model`, `agent.max_steps`, and `agent.base_url` in [`config/nika.yaml`](../operations/configuration.md).

For sandboxed agents, the endpoint must be reachable from the sandbox; its `localhost` is separate from the host's. Claude-family agents also require an Anthropic-compatible API. See [endpoint compatibility](agent-reference.md#custom-endpoint-compatibility) before adapting these examples.

## Inspect results

```shell
uv run nika inspect
```

![NIKA Inspect showing an agent's tool calls and router output](../../assets/images/nika-inspect-timeline.png)

Open your session to follow the investigation in **Timeline**, inspect tool inputs and outputs, and compare the answer with ground truth in **Scores**. See [Inspect](../operations/session-inspect.md) for dashboard details and [root-cause scoring](../benchmarks/root-cause-evaluation.md) for how answers are evaluated.

For metrics in the terminal:

```shell
uv run nika eval metrics --session_id <session_id>
```

Runs with `--problem` manage lab cleanup. If you instead use an existing lab, close the session you created when finished:

```shell
uv run nika session close --session_id <session_id> -y
```
