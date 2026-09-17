<div align="center">

<img src="./assets/images/nika-banner.svg" alt="NIKA" width="100%"/>

<br />

[✨ Features](#-features) ·
[🤖 Overview](#-overview) ·
[📦 Installation](#-installation) ·
[🚀 Quick start](#-quick-start) ·
[📖 Learn more](#-learn-more) ·
[📄 Docs](docs/README.md) ·
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

## ✨ Features

- **Live network incidents:** Inject faults into running data center, campus, ISP, SDN, P4, and Kubernetes labs. NIKA supports [Kathará](https://www.kathara.org/) and [Containerlab](https://containerlab.dev/) backends.
- **Evidence from the network:** Agents can use [MCP tools](#mcp-servers) for host probes, router and switch commands, packet capture, Pingmesh, and scenario-specific INT telemetry.
- **Root-cause evaluation:** Score submitted resource and fault-type IDs against [benchmark ground truth](docs/benchmarks/root-cause-evaluation.md). Cases also include healthy controls and multiple faults.
- **Your choice of agent:** Run a registered agent or [integrate your own](docs/agents/custom-agents.md); supported agents can run in a [sandbox](docs/operations/agent-sandbox.md).
- **Comparable runs:** Use [frozen releases](docs/benchmarks/benchmark-configuration.md) with published cases and Dev/Test splits, then [submit results](docs/benchmarks/leaderboard-submission.md).
- **Flexible lab placement:** Run isolated sessions in parallel or move labs to a [remote host](docs/operations/remote.md).
- **Extensible benchmark:** [Add scenarios and failures](docs/development/creating-benchmark-tasks.md) through NIKA's existing interfaces.

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

### Network types

Choose a lab that matches the network you want to troubleshoot. The [network scenario reference](docs/operations/network-scenarios.md) lists each scenario's backend and requirements.

| Network | Example scenarios | What they run |
| --- | --- | --- |
| Data center | [`dc_clos`](docs/operations/network-scenarios.md#data-center-clos-scenario), [`min3clos`](docs/operations/network-scenarios.md#min3clos) | Routed Clos fabrics on FRR or Nokia SR Linux |
| Campus | [`campus_lan`](docs/operations/network-scenarios.md#campus-lan-scenario) | OSPF campus network with DHCP, DNS, and web services |
| Enterprise WAN | [`enterprise_branch`](docs/operations/network-scenarios.md#enterprise_branch) | Provider underlay and WireGuard/eBGP overlay |
| ISP | [`isp_abilene`](docs/operations/network-scenarios.md#sndlib-isp-scenarios), [`isp_france`](docs/operations/network-scenarios.md#sndlib-isp-scenarios), etc. | SNDlib backbone topologies with configurable routing |
| SDN and P4 | [`sdn_l3_clos`](docs/operations/network-scenarios.md#sdn_l3_clos), [`p4_dc_fabric`](docs/operations/network-scenarios.md#p4_dc_fabric), [`p4_dc_gateway`](docs/operations/network-scenarios.md#p4_dc_gateway) | ONOS/OVS and BMv2 fabrics |
| Kubernetes | [`k8s_lab`](docs/operations/network-scenarios.md#k8s_lab), [`llmd_lab`](docs/operations/network-scenarios.md#llmd_lab) | k3s workloads in network labs |
| Vendor routing | [`iosxr_simple_bgp`](docs/operations/network-scenarios.md#iosxr_simple_bgp) | Cisco XRd eBGP lab |


### Network incidents

The table summarizes registered failure types and working-matrix cases by [failure domain](docs/operations/failures.md).

| Failure domain | Registered failure types | Working-matrix cases |
| --- | ---: | ---: |
| Link & Interface | 6 | 267 |
| Routing & Control Plane | 8 | 196 |
| Forwarding, Encapsulation & Policy | 26 | 324 |
| Service Networking | 6 | 17 |
| Management & Orchestration Plane | 4 | 11 |
| Addressing, Neighbor & Naming | 13 | 132 |
| Endpoint & Application | 2 | 38 |
| Traffic, Queueing & Resource | 3 | 25 |
| Security | 7 | 88 |
| **Total** | **75** | **1,098** |

Run `uv run nika failure describe <failure_id>` to see a failure's injection parameters. The [failure reference](docs/operations/failures.md#registered-failures) lists every ID and its verification contract.

### MCP servers

NIKA mounts host diagnostics, Pingmesh, and packet capture for every diagnosis session. It adds other servers according to the scenario and backend. Submission uses a separate server after diagnosis.

| Server | Available in | Main use |
| --- | --- | --- |
| `kathara_base_mcp_server` | All scenarios, on either backend | Host probes, network configuration, and shell commands |
| `pingmesh_mcp_server` | All scenarios | On-demand endpoint reachability, packet loss, and RTT |
| `packet_capture_mcp_server` | All scenarios | Start, stop, and inspect packet captures |
| `kathara_frr_mcp_server` | Kathará routing scenarios | FRR routes, configuration, and BGP/OSPF state |
| `kathara_iosxr_mcp_server` | `iosxr_simple_bgp` | IOS-XR configuration, routes, and CLI commands |
| `kathara_bmv2_mcp_server` | Kathará P4 scenarios | BMv2 switch state through P4Runtime |
| `kathara_sdn_mcp_server` | `sdn_l3_clos` | ONOS and OVS state and commands |
| `kathara_telemetry_mcp_server` | `p4_dc_gateway` on Kathará | Observed INT-MX packet paths and hop data |
| `k8s_mcp_server` | Kubernetes scenarios when MCP access is enabled | Nodes, pods, services, logs, and connectivity |
| `containerlab_srl_mcp_server` | Containerlab routing scenarios | SR Linux routes, BGP state, configuration, and CLI commands |
| `task_mcp_server` | Submission phase of every session | Submit the agent's root-cause diagnosis |

See [MCP servers](docs/agents/mcp-servers.md) for the full tool list and session access rules.


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
| **[NIKA](https://sands-lab.github.io/nika)** | Live network troubleshooting | ⭐️⭐️⭐️ <br> 75 registered fault types <br> 40 scenario IDs | ⭐️⭐️ <br> 1,098 incident variants | ⭐️⭐️⭐️ <br> ✔ Kathará/Containerlab emulation <br> ✔ Vendor CLIs & telemetry tools | 🟢 Online | Agentic evals |
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
