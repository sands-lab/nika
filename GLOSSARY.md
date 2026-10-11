# Glossary

Terms used across NIKA's documentation. Pick the term that matches what the reader is doing; when two terms overlap, the definitions below say how they relate.

## Agents

| Term | Meaning |
| --- | --- |
| **Agent** | What NIKA evaluates: a **model** plus the **harness** that drives it. Developers connect, run, and compare agents. |
| **Model** | The LLM that reasons, such as `gpt-5-mini` or `Qwen3.6-35B-A3B`, reached through an LLM **provider**. |
| **Harness** | The code around the model: the investigation loop, prompts, tool access, and step limits. It can be a thin wrapper or a full framework such as LangGraph or Claude Code. Use it when the code around the model is the subject, for example when comparing harnesses with the same model. |
| **Provider** | Where NIKA sends model requests: `openai`, `anthropic`, `deepseek`, or `custom` for a self-hosted endpoint. |
| **MCP tools** | The tools an agent uses to inspect the lab, such as running commands on devices or a Pingmesh snapshot. NIKA serves them through the MCP gateway. |

## Runs

| Term | Meaning |
| --- | --- |
| **Lab** | A deployed, running network, on Kathará or Containerlab. |
| **Scenario** | A network design NIKA can deploy as a lab, such as `dc_clos`. |
| **Failure** (fault) | A root cause NIKA injects into a lab, such as `link_down`. |
| **Session** | One lab with its injected failures and results directory, from deployment until it is closed. |
| **Diagnosis** | The run phase in which the agent investigates the lab and writes a free-text report. |
| **Submission** | The run phase in which the agent turns its frozen report into a structured answer: the failed resources and fault types. |
| **Ground truth** | The failures NIKA actually injected, which the submission is scored against. |

## Benchmarks

| Term | Meaning |
| --- | --- |
| **Case** | One scenario, deployment profile, and injected failure, identified by a task id such as `dc_clos_s_link_down`. |
| **Trial** | One scored run of a case. A release runs each case several times. |
| **Release** | A frozen, versioned set of cases, such as `0.2.0`. Published releases do not change. |
