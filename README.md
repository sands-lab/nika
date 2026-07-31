<div align="center">

<img src="./assets/images/nika-banner.svg" alt="NIKA" width="100%"/>

<br />

[🤖 Overview](#-overview) ·
[📦 Installation](#-installation) ·
[🚀 Quick start](#-quick-start) ·
[📖 Learn more](#-learn-more) ·
[🌐 Website](https://sands-lab.github.io/nika/) ·
[📚 Cite](#-citation)

[![ArXiv Link](https://img.shields.io/badge/arXiv-2512.16381-red?logo=arxiv)](https://arxiv.org/abs/2512.16381)
[![Project Page](https://img.shields.io/badge/-Project%20Page-1E88E5?logo=googlechrome&logoColor=white&labelColor=24292f)](https://sands-lab.github.io/nika/)
[![Open Telco AI](https://img.shields.io/badge/-Open%20Telco%20AI-00AEEF?logo=gsma&logoColor=white&labelColor=24292f)](https://www.open-telco.ai/resources/nika/)

</div>

## ❓ What is NIKA?

Think about [SWE-Bench](https://github.com/swe-bench/SWE-bench), but for network troubleshooting. [NIKA](https://sands-lab.github.io/nika/), **N**etwork **I**ncident Benchmar**k** for **A**I Agents, is an *open benchmark for agentic evals on network troubleshooting tasks*. It connects any agent directly to a live network stack: routers, switches, hosts, and telemetry tools, all running on general-purpose compute. NIKA reproduces hundreds of realistic faults covering data center networks, campus networks, ISP backbones, SDN fabrics, overlay networks, and Kubernetes CNIs.

## 🙋 Why NIKA?

NIKA lets you plug in any LLM or agent framework and measure its operational capability under identical, reproducible conditions.

It helps different users answer questions like:

- 💬 **Network Manager** "A vendor is pitching me an AI solution for network operations. It passes all the standard telecom benchmarks (TeleQnA, TeleLogs, TeleMath, 3GPP-TSG), but I need objective evidence it can handle real incidents before I sign off."
- 💬 **Network Engineer and SREs** "I respond to network incidents every day. I want an AI agent to help, but I'm not sure it will understand my topology or make things worse."
- 💬 **AI Researcher** "I'm designing a new agent architecture for long-horizon network tasks. I need a benchmark to ablate components, measure reproducibly, and compare against published baselines."
- 💬 **AI/ML Engineer** "I want to fine-tune an open-source model on network troubleshooting and need a structured dataset paired with a rigorous evaluation framework."
- 💬 **Open-Source Contributor** "I want to contribute a new network scenario or fault type to the community and have it evaluated systematically."

## 🤖 Overview

![NIKA Architecture](./assets/images/architecture.png)

NIKA combines two components:

1. **NIKA Benchmark** — a suite of reproducible incidents defined by a network scenario and an injectable root cause. The full working matrix contains 15 scenarios, 56 fault types, and 702 cases.
2. **NIKA Orchestrator** — a modular platform that deploys live labs, injects faults, connects agents to interactive MCP tools, and evaluates their submissions.

NIKA supports Kathará and Containerlab backends, multiple agent frameworks, native CLI agents, sandboxed execution, and MCP-based network diagnostics. The implementation and configuration details are linked in [Learn more](#-learn-more).

### Network operations benchmark landscape

| Benchmark                                                             | Type                | Supported scenarios                                                                 | Evaluation environment                                           | Supported agent types                                              | Agent isolation                                                | Parallel execution    |
| --------------------------------------------------------------------- | ------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------- | --------------------- |
| **NIKA**                                                              | **Dynamic**         | Data center, campus, ISP, SDN, P4, overlay, and Kubernetes | Live Kathará and Containerlab labs; MCP tools                              | LangGraph, AutoGen, MCP Agent, custom Python agents, Codex, Claude | [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/get-started/)   | ✓       |
| [NetOpsBench](https://github.com/NetX-lab/NetOpsBench)                | **Dynamic**         | Data-center leaf-spine fabrics at XS, Small, Medium, and Large scales                                    | Live Containerlab and SONiC-VS; MCP and telemetry tools                    | Custom Python troubleshooting agents                               | Not documented                                                 | ✓            |
| [NetArena](https://github.com/Froot-NetSys/NetArena)                  | **Dynamic**         | Data-center capacity planning, routing, and Kubernetes microservice-policy scenarios                     | Dynamically generated emulation and Kubernetes environments; A2A interface | A2A-compatible agents                                              | Optional Docker deployment                                     | Not documented        |
| [AIOpsLab](https://github.com/microsoft/AIOpsLab)                     | **Dynamic**         | Kubernetes microservices with injected application and infrastructure faults                             | Live Kubernetes applications, workloads, telemetry, and orchestrator APIs  | Custom Python and remote agents                                    | Not documented                                                 | Not documented        |
| [ITBench](https://github.com/itbench-hub/ITBench)                     | **Dynamic**         | Kubernetes-based SRE, security, compliance, and FinOps scenarios                                         | Managed live environments with natural-language tools                      | Externally operated agents                                         | Agents run externally                                          | Not documented        |
| [Cornetto](https://arxiv.org/abs/2604.22513)                          | **Static**          | Synthesized networks with 20–754 nodes; 231 configuration-repair problems                                | Offline formal network verification                                        | LLMs and configuration-repair methods                              | Not applicable                                                 | Not documented        |
| [NetConfEval](https://github.com/RedHatResearch/conext24-NetConfEval) | **Static**          | Routing, API generation, formal specifications, and device configurations                                | Pre-generated tasks with offline validation                                | Configurable LLM backends                                          | Not applicable                                                 | Not documented        |
| [GSMA Open Telco](https://huggingface.co/datasets/GSMA/ot-full)       | **Static**          | Telecom knowledge, standards, logs, O-RAN, srsRAN, and 6G tasks                                          | 20,588 samples across eight datasets                                       | Models supported by Inspect AI                                     | Not applicable                                                 | Not documented        |
| [WirelessBench](https://wirelessbench.github.io/)                     | **Static**          | Wireless calculations, 5G slicing, and mobility-aware allocation                                         | 848 validation and 2,544 test samples; offline domain tools                | LLMs and reference agents                                          | Not applicable                                                 | Not documented        |
| [TeleLogsAgent](https://huggingface.co/datasets/netop/TeleLogsAgent)  | **Static tool-use** | 5G drive-test, radio KPI, mobility, signaling, and throughput scenarios                                  | Fixed data exposed through HTTP or MCP tools                               | OpenAI-compatible models and MCP agents                            | Not documented                                                 | Not documented        |
| [NetInjectBench](https://arxiv.org/abs/2607.10490)                    | **Static tool-use** | 130 benign, prompt-injection, and approved-change network scenarios                                      | Six simulated network-operations tools                                     | Evaluated models and defense methods                               | Not applicable                                                 | Not documented        |
| [Multi-modal Wi-Fi Fault Diagnosis](https://arxiv.org/abs/2605.22008) | **Static**          | Campus Wi-Fi testbed and 11 cross-layer fault categories                                                 | More than 10,000 samples collected from a campus Wi-Fi testbed             | Multi-modal diagnostic models                                      | Not applicable                                                 | Not documented        |

`Dynamic` benchmarks let agents observe or modify a running environment. `Static` benchmarks evaluate pre-collected or generated samples. `Static tool-use` benchmarks use fixed scenarios but expose them through simulated or local tools.


## 📦 Installation

NIKA requires Linux and Python 3.12+. Dependency management uses [uv](https://docs.astral.sh/uv/).

Core install (agent CLI and evaluation; no local lab backends):

```shell
git clone https://github.com/sands-lab/nika
cd nika
uv sync
source .venv/bin/activate
cp .env.example .env
```

Local lab deployment needs Docker plus at least one backend extra:

| Extra | Provides | Also requires |
|-------|----------|---------------|
| `kathara` | [Kathará](https://www.kathara.org/) Python API, Docker SDK, Kubernetes client | Docker |
| `containerlab` | Docker SDK for [Containerlab](https://containerlab.dev/) labs | Docker, `clab` on `PATH` (`gnmic` for SRL) |
| `labs` | Both of the above | Same as each backend |

```shell
uv sync --extra labs          # local Kathara + Containerlab
# or: uv sync --extra kathara
# or: uv sync --extra containerlab
```

Add the model credentials and agent settings you need to `.env`. CLI flags override `.env` values. Sandbox-backed agents also require `sbx login`, KVM, and the appropriate optional dependency group; see the [agent sandbox guide](docs/agent-sandbox.md).

To run labs on a separate machine, see [NIKA Remote](docs/remote.md).

## 🚀 Quick start

The following commands run one incident from deployment through evaluation:

```shell
# Discover and deploy a live lab. This prints a session_id.
nika env list
nika env run simple_bgp

# Inspect the fault schema and inject a reproducible link failure.
nika failure describe link_down
nika failure inject link_down --set host_name=pc1 --set intf_name=eth0

# Run the native Claude Code troubleshooting agent against the live lab.
nika agent list
nika agent run -a cli.claude

# Close the lab and score the submission.
nika session close -y
nika eval metrics
nika eval judge -p openai -m gpt-5-mini
```

When multiple sessions are active, pass `--session_id <id>` to session-scoped commands.

To run the frozen benchmark release instead:

```shell
nika benchmark run --release 0.1.0 --result_dir results/my-run --batch-size 4
nika eval summary --result_dir results/my-run
```

Benchmark runs deploy the lab, inject the fault, run the selected agent, close the session, and compute rule-based metrics. LLM judging remains an explicit post-processing step.

## 📖 Learn more

**Run and evaluate**

- [CLI reference](src/nika/cli/README.md) — commands, options, sessions, and result paths.
- [Benchmark guide](benchmark/README.md) — releases, custom cases, parallel execution, and resume behavior.
- [Testing guide](tests/README.md) — test suites and their external prerequisites.

**Build and extend**

- [Agent architecture](src/agent/README.md) — built-in agents and configuration.
- [Custom agents](docs/custom-agents.md) — agent contract and integration workflow.
- [Agent sandbox](docs/agent-sandbox.md) — isolated microVM execution.
- [Agent skills](docs/agent-skills.md) — reusable troubleshooting knowledge.
- [Creating benchmark tasks](docs/creating-benchmark-tasks.md) — scenarios, faults, traffic, and cases.

**Share results**

- [Leaderboard submission](docs/leaderboard-submission.md) — package, validate, and submit an official run.
- [NIKA Zenodo dataset](https://zenodo.org/records/17971675) — public agent troubleshooting traces.

The complete environment template is [`.env.example`](.env.example).

## 📚 Citation

If you use NIKA in your research, please cite:

```bibtex
@misc{nika,
  title        = {A Network Arena for Benchmarking AI Agents on Network Troubleshooting},
  author       = {Zhihao Wang and Alessandro Cornacchia and Alessio Sacco and Franco Galante and Marco Canini and Dingde Jiang},
  year         = {2025},
  eprint       = {2512.16381},
  archivePrefix = {arXiv},
  primaryClass = {cs.NI},
  url          = {https://arxiv.org/abs/2512.16381}
}
```

NIKA is also described in the [NGNO '25 paper](https://doi.org/10.1145/3748496.3748990).

## 🙏 Acknowledgement

NIKA is motivated in part by [AIOpsLab](https://github.com/microsoft/AIOpsLab). We thank its authors for their work.

## 📄 License

NIKA is released under the MIT License.
