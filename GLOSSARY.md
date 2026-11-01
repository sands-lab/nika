# Glossary

Terms used across NIKA's documentation. Pick the term that matches what the reader is doing; when two terms overlap, the definitions below say how they relate.

A NIKA benchmark is a set of **tasks**. Each task combines a **network environment**, a **network incident**, and a **network traffic pattern**, and asks an agent to troubleshoot the incident in the live environment.

## Benchmarks

| Term | Meaning |
| --- | --- |
| **Task** | A combination of a network environment, a network incident, and a network traffic pattern, identified by a task ID such as `dc_clos_s_link_down`. Code, configuration files, and some commands call it a *case* (`cases:`, `nika case browse`). Prefer “task” in prose. |
| **Network environment** | A network design NIKA can deploy and run, such as the data center Clos `dc_clos`, on Kathará or Containerlab. Code and CLI options call the design a *scenario* (`scenario:`, `nika env run`), and older documentation calls the running deployment a *lab*. Prefer “environment”; “scenario” is an acceptable alternative. |
| **Network incident** | The problem the agent must troubleshoot, caused by one or more injected failures. A healthy task has no failure and checks that the agent does not report one. |
| **Network traffic pattern** | The traffic that flows through the environment while the incident is live, such as service requests, SNDlib demand replay on an ISP backbone, or synchronized bursts toward one receiver. Every environment runs its business traffic; a task can add a background traffic profile with `traffic:`. Some failures start traffic of their own. |
| **Telemetry** | Optional per-task monitoring data that devices in the environment export to a collector the agent can query: flow records (NetFlow, IPFIX, or sFlow), device metrics (Prometheus), and syslog. A task enables it with `telemetry:`; see [telemetry](docs/operations/network-scenarios.md#telemetry). |
| **Trial** | One scored run of a task. A task can be repeated across multiple trials. |
| **Benchmark release** | A published version of the benchmark tasks, such as `0.2.0`, split into Dev and Test sets. Published releases do not change; the [versioning policy](VERSIONING.md) defines how versions are numbered. |
| **Benchmark revision** | The first two components of a release version, such as `1.0` in `1.0.3`. It identifies the tasks and evaluation that results were scored against, and the leaderboard shows it. |

## Runs

| Term | Meaning |
| --- | --- |
| **Failure** (fault) | A root cause NIKA injects into an environment, such as `link_down`. Code and configuration may call the failure type a *problem* (`problem:`); the `--problem` option of `nika agent run` instead selects a task label. |
| **Session** | One deployed environment with its injected failures and results directory, from deployment until it is closed. |
| **Diagnosis** | The run phase in which the agent investigates the environment and writes a free-text report. |
| **Submission** | The run phase in which the agent turns its frozen report into a structured answer: the failed resources and fault types. |
| **Ground truth** | The failures NIKA actually injected, which the submission is scored against. |
| **Oracle solver** | The `oracle` agent. It knows the ground truth and submits it only after the failure's playbook confirms the fault through the agent's MCP tools. See [Oracle solver](docs/benchmarks/oracle-solver.md). |
| **Playbook** | A failure's reference diagnosis: the MCP tool calls an operator would make, the observation each must show, and which observations are evidence for the root cause. |
| **Leaderboard submission** | One result package submitted to the leaderboard: the scores of one agent and model on a benchmark release, with its trajectories. Not the *Submission* run phase. |
| **Audit issue** | A confirmed defect in the benchmark, its harness, or the runs of a leaderboard submission, such as a shortcut or a tool that always fails. The [submission audit](.agents/skills/nika-submission-audit/SKILL.md) records it; it is not an injected failure. |

## Agents

| Term | Meaning |
| --- | --- |
| **Agent** | What NIKA evaluates: a **model** plus the **harness** that drives it. Developers connect, run, and compare agents. |
| **Model** | The LLM that reasons, such as `gpt-5-mini` or `Qwen3.6-35B-A3B`, reached through an LLM **provider**. |
| **Harness** | The code around the model: the investigation loop, prompts, tool access, and step limits. It can be a thin wrapper or a full framework such as LangGraph or Claude Code. Use it when the code around the model is the subject, for example when comparing harnesses with the same model. |
| **Provider** | Where NIKA sends model requests: `openai`, `anthropic`, `deepseek`, or `custom` for a self-hosted endpoint. |
| **MCP tools** | The tools an agent uses to inspect the environment, such as running commands on devices or a Pingmesh snapshot. NIKA serves them through the MCP gateway. |
