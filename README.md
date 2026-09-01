<div align="center">

<img src="./assets/images/nika-banner.svg" alt="NIKA" width="100%"/>

<br />

[🤖 Overview](#-overview) ·
[✨ Features](#-features) ·
[📦 Installation](#-installation) ·
[🚀 Quick start](#-quick-start) ·
[📖 Learn more](#-learn-more) ·
[🌐 Website](https://sands-lab.github.io/nika/) ·
[📚 Cite](#-citation)

[![ArXiv Link](https://img.shields.io/badge/arXiv-2512.16381-red?logo=arxiv)](https://arxiv.org/abs/2512.16381)
[![Project Page](https://img.shields.io/badge/-Project%20Page-1E88E5?logo=googlechrome&logoColor=white&labelColor=24292f)](https://sands-lab.github.io/nika/)
[![Open Telco AI](https://img.shields.io/badge/-Open%20Telco%20AI-00AEEF?logo=gsma&logoColor=white&labelColor=24292f)](https://www.open-telco.ai/resources/nika/)

</div>

## 📰 News

- **2026-09-01:** Published [nika-bench 0.2.0](benchmark/releases/0.2.0/README.md). `nika leaderboard submit` now packs agent trajectories and opens a Hugging Face dataset PR; see [leaderboard submission](docs/benchmarks/leaderboard-submission.md).
- **2026-08-15:** Operational settings moved fully to `config/nika.yaml`. New installations can copy `config/nika.example.yaml`; existing installations with operational `.env` keys can run [`nika config migrate`](docs/operations/cli-reference.md#nika-config).
- **2026-08-13:** Updated benchmark labels and evaluation. Users with older custom benchmark YAML can [migrate their case matrices](docs/benchmarks/root-cause-evaluation.md#materialize-labels-on-a-case-matrix).

## ❓ What is NIKA?

Think about [SWE-Bench](https://github.com/swe-bench/SWE-bench), but for network troubleshooting. [NIKA](https://sands-lab.github.io/nika/), **N**etwork **I**ncident Benchmar**k** for **A**I Agents, is an *open benchmark for agentic evals on network troubleshooting tasks*. NIKA reproduces hundreds of realistic faults covering data center networks, campus networks, ISP backbones, SDN fabrics, overlay networks, and Kubernetes CNIs. It connects any agent directly to a live network stack while the incident is ongoing, evaluating the ability of the AI agent to troubleshoot the network using network diagnostic tools, switch CLIs, and network telemetry data. You don't need physical hardware to run the benchmark, NIKA is powered by state-of-the-art network emulation backends like [Kathará](https://www.kathara.org/) and [Containerlab](https://containerlab.dev/), so you can run it on your laptop or in the cloud.

## 🙋 Why NIKA?

NIKA lets you plug in any LLM or agent framework and measure its operational capability under identical, reproducible conditions.

It helps different users answer questions like:

- 💬 **Network Manager** "A vendor is pitching me an AI solution for network operations. It passes all the standard telecom benchmarks (TeleQnA, TeleLogs, TeleMath, 3GPP-TSG), but I need objective evidence it can handle real incidents before I sign off."
- 💬 **Network SRE** "I respond to network incidents every day. I want an AI agent to help, but I'm not sure it will understand my topology or make things worse."
- 💬 **AI Researcher** "I'm designing a new harness for long-horizon network tasks. I need a benchmark to ablate components, measure reproducibly, and compare against published baselines."
- 💬 **Applied ML Engineer** "I want to fine-tune an open-source model on network troubleshooting and need a structured dataset paired with a rigorous evaluation framework."
- 💬 **Contributor** "I want to contribute a new network scenario or fault type to the community and have it evaluated systematically."

## 🤖 Overview

![NIKA Architecture](./assets/images/architecture.png)

NIKA combines two components:

1. **NIKA Benchmark** — a suite of reproducible incidents defined by a network scenario and an injectable root cause.
2. **NIKA Orchestrator** — a modular platform that deploys live labs, injects faults, connects agents to interactive MCP tools, and evaluates their submissions.

### Network incidents

NIKA constructs benchmark incidents from recurring failure mechanisms. The [failure taxonomy](docs/operations/failures.md) uses a network-subsystem domain plus orthogonal cause, symptom, scope, temporal, and impact metadata. Failure IDs and injection behavior remain stable across taxonomy changes.

| Failure domain | Registered failure types | Working-matrix cases |
| --- | ---: | ---: |
| Link & Interface | 5 | 191 |
| Routing & Control Plane | 9 | 233 |
| Forwarding, Encapsulation & Policy | 25 | 309 |
| Service Networking | 6 | 17 |
| Management & Orchestration Plane | 4 | 11 |
| Addressing, Neighbor & Naming | 17 | 161 |
| Endpoint & Application | 5 | 96 |
| Traffic, Queueing & Resource | 4 | 72 |
| **Total** | **75** | **1,090** |

Run `uv run nika failure describe <failure_id>` to inspect the taxonomy metadata and injection parameter schema. The [failure reference](docs/operations/failures.md#registered-failures) lists all 75 IDs with their injection and verification contracts.


## ✨ Features

- **Network emulators**: NIKA attaches to state-of-the-art network emulators as backends. Are you a [Kathará](https://www.kathara.org) or [Containerlab](https://containerlab.dev) user? You can use NIKA with both.
- **Pre-built incident scenarios**: Running your evals is quite simple: start any of the pre-built network scenarios in the NIKA benchmark, with automatic incident replay and evaluation mechanisms.
- **Bring any AI agent**: You can use our default agents (Claude Code, Codex, LangGraph), or plug your custom AI agent harness, see [Agent integration workflow](docs/agents/custom-agents.md).
- **Agent sandboxing**: Agents run in isolated environments, with controlled access to the network, filesystem and telemetry tools, see [Agent sandboxing](docs/operations/agent-sandbox.md).
- **YAML-based fault injection**: Failures can be customized via a declarative interface: `nika failure describe`, and later `--set key=value`.
- **MCP network telemetry**: Pingmesh server, InfluxDB network telemetry and CLI access to routers and switches.
- **Multi-session**: Run isolated sessions in parallel to speed up your evaluations.
- **Remote execution mode**: Run the emulated network and telemetry MCP gateways on any remote server, see [NIKA Remote](docs/operations/remote.md).
- **Reproducibility and leaderboard**: Refer to the frozen `nika-bench` releases, and submit your results to our up-to-date leaderboard.
- **NIKA SDK**: For users who wish to extend with new failure cases using NIKA's APIs for traffic generation and fault injection, see [Creating benchmark tasks](docs/development/creating-benchmark-tasks.md).


## 📦 Installation

**Requirements**: Python 3.12+, and [uv](https://docs.astral.sh/uv/) for dependency management. Additionally, NIKA needs Docker and at least one network emulation backend. Currently supported backends are:

- **[Kathará](https://www.kathara.org/)** — install with `--extra kathara` option below.
- **[Containerlab](https://containerlab.dev/)** — install with `--extra containerlab` option below.
- **Both** — install with `--extra labs` option below.

`switch_internal_packet_corruption` also needs controller-host eBPF build
tooling. On Debian or Ubuntu, install it with:

```shell
sudo apt-get update
sudo apt-get install -y clang iproute2
```

This is a controller-host prerequisite. It is not installed in lab nodes or
Agent sandboxes.

### Basic setup

```shell
git clone https://github.com/sands-lab/nika
cd nika
uv sync --extra labs   # or --extra kathara / --extra containerlab / (no extra)
source .venv/bin/activate
cp .env.example .env
```

### API keys and credentials

Keys live in `.env`; agent/benchmark settings live in `config/nika.yaml` (CLI flags override YAML). Copy the templates, then edit:

```shell
cp .env.example .env
cp config/nika.example.yaml config/nika.yaml
nika config show
```

If an existing `.env` contains operational settings, run `nika config migrate` instead. See the [run configuration reference](docs/operations/configuration.md) for precedence, defaults, and validation rules.

**Provider** — use a built-in provider (`openai` / `anthropic` / `deepseek`). Put the matching API key in `.env`, and set `agent.provider` in YAML:

```shell
# .env
OPENAI_API_KEY=...          # or ANTHROPIC_API_KEY / DEEPSEEK_API_KEY

# config/nika.yaml
agent:
  provider: openai          # or anthropic / deepseek
```

**Custom** — use any OpenAI-compatible endpoint (OpenRouter / Ollama / vLLM / …). Put the key in `.env` (omit if unauthenticated), and set `base_url` (and optional `model`) under `agent.custom` in YAML:

```shell
# .env
NIKA_CUSTOM_API_KEY=...     # optional if the endpoint needs no auth

# config/nika.yaml
agent:
  provider: custom
  custom:
    base_url: https://openrouter.ai/api/v1
    model: null
```

### Remote Deployments:

- **Agent Sandboxing**: See the [agent sandbox guide](docs/operations/agent-sandbox.md) for sandboxed execution requirements.
- **Remote Mode**: Use [remote lab execution](docs/operations/remote.md) to run the emulated network and telemetry MCP gateways on a separate server while the agent runs locally.

## 🚀 Quick start

Run one incident end-to-end with a task label (`{scenario}_{problem}`, or `{scenario}_{s|m|l}_{problem}` when the scenario is sized):

```shell
nika agent list
nika agent run -a byo.langgraph -p openai -m gpt-5-mini \
  --problem dc_clos_s_link_down
```

That deploys the lab, injects the fault, runs the agent, closes the session, and writes evaluation results.

To run a frozen benchmark release:

```shell
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run --batch-size 4
nika eval summary --result_dir results/my-run
```


For lab control (`env` / `failure` / `session`), inject parameter overrides, and the full command tree, see the [CLI reference](docs/operations/cli-reference.md).

## 📖 Learn more

Pick the path that matches what you're trying to do:

**🏁 I want to run the benchmark, any agent**

1. [Quick start](#-quick-start) — end-to-end task run or frozen release.
2. [Run configuration](docs/operations/configuration.md): YAML settings, credentials, defaults, and migration.
3. [CLI reference](docs/operations/cli-reference.md): `nika` commands, sessions, and result paths.
4. [Leaderboard submission](docs/benchmarks/leaderboard-submission.md) (GitHub scores + Hugging Face trajectories)

**🔌 I want to connect my own agent**

1. [Built-in agents](docs/agents/agent-implementations.md): built-in agents and configuration.
2. [Agent integration workflow](docs/agents/custom-agents.md): agent contract and integration workflow.
3. [Agent skills](docs/agents/agent-skills.md): reusable troubleshooting knowledge you can attach to an agent.
4. [Agent sandboxing](docs/operations/agent-sandbox.md): isolated microVM execution.

**🌐 I want to create a new network scenario**

1. [Creating benchmark tasks](docs/development/creating-benchmark-tasks.md)
2. [Network scenario reference](docs/operations/network-scenarios.md)
3. [Failure reference](docs/operations/failures.md)
4. [Testing guide](docs/development/testing.md)


## Network management benchmarks

NIKA is part of a growing ecosystem. The table below compares NIKA with other benchmarks in terms of their focus, agent interactivity, variety, scale, and realism. While the best benchmark depends on your use case, NIKA currently outstands  for realistic agentic evaluations in online environments**.

| Benchmark | Description | Variety | Scale | Environment Realism | Type | Best for |
|---|---|:---:|:---:|:---:|:---:|---|
| **[NIKA](https://sands-lab.github.io/nika)** | Live network troubleshooting | ⭐️⭐️⭐️ <br> 75 registered fault types <br> 10 scenario IDs | ⭐️⭐️ <br> 1,090 incident variants | ⭐️⭐️⭐️ <br> ✔ Kathará/Containerlab emulation <br> ✔ Vendor CLIs & telemetry tools | 🟢 Online | Agentic evals |
| [NetOpsBench](https://github.com/NetX-lab/NetOpsBench) | Live network troubleshooting | ⭐️ <br> 13 fault types <br> 1 network type | ⭐️⭐️ <br>~600 incident variants | ⭐️⭐️⭐️ <br> ✔ Containerlab emulation <br> ✔ Vendor CLIs & telemetry tools | 🟢 Online | Agentic evals |
| [NetArena](https://github.com/Froot-NetSys/NetArena) | Network operations | ⭐️ <br> 3 setups, 5 fault types | ⭐️⭐️⭐️ <br> ~9,000 variants | ⭐️⭐️ <br>Mininet <br> Basic netutils (e.g., ping) | 🟢 Online | Large-scale synthetic variants for ML |
| [NetConfEval](https://github.com/RedHatResearch/conext24-NetConfEval) | Basic network configuration | ⭐️ <br> Reachability, waypoint, load balancing on 8x topologies | ⭐️⭐️⭐️ <br> ~3,000 variants | ⭐️ <br> Simple offline validator | 🔴 Offline / Static | Basic LLM config-generation capability |
| [Cornetto](https://arxiv.org/abs/2604.22513) | Config-repair with formal verification | ⭐️⭐️ <br> 50 fault types, misconfigurations only | ⭐️⭐️ <br> 231 scenarios, 20-754x topology size | ⭐️⭐️ <br> Batfish | 🔴 Offline / Static | Basic LLM config-fix capability |
| [GSMA Open Telco](https://huggingface.co/datasets/GSMA/ot-full) | Q&A telecom knowledge | ⭐️⭐️ <br> Multiple telecom datasets | ⭐️⭐️⭐️ <br> 20,588 samples | ⭐️ <br> Simple offline validator | 🔴 Offline / Static | Basic LLM telecom knowledge |

**Notes:** `Type=Online` indicates that agents can observe, modify and interact with a live network environment while running. `Offline` benchmarks evaluate pre-collected (or generated) samples.

## 📚 Citation

If you use NIKA in your research, please cite:

```bibtex
@misc{nika25long,
  title          = {A Network Arena for Benchmarking AI Agents on Network Troubleshooting},
  author         = {Zhihao Wang and Alessandro Cornacchia and Alessio Sacco and Franco Galante and Marco Canini and Dingde Jiang},
  year           = {2025},
  eprint         = {2512.16381},
  archivePrefix  = {arXiv},
  primaryClass   = {cs.NI},
  url            = {https://arxiv.org/abs/2512.16381}
}
```

Please also cite our [NGNO '25 paper](https://doi.org/10.1145/3748496.3748990):

```bibtex
@inproceedings{nika25ngno,
  title        = {Towards a Playground to Democratize Experimentation and Benchmarking of AI Agents for Network Troubleshooting},
  author       = {Wang, Zhihao and Cornacchia, Alessandro and Galante, Franco and Centofanti, Carlo and Sacco, Alessio and Jiang, Dingde},
  year         = {2025},
  publisher    = {Association for Computing Machinery},
  url          = {https://doi.org/10.1145/3748496.3748990},
  booktitle    = {Proceedings of the 1st Workshop on Next-Generation Network Observability},
  location     = {Coimbra, Portugal},
  series       = {NGNO '25}
}
```

## 🙏 Acknowledgement

We thank the authors of [AIOpsLab](https://github.com/microsoft/AIOpsLab) for their useful feedbacks.

## 📄 License

NIKA is released under the MIT License.
