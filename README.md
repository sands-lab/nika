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

1. **NIKA Benchmark** — a suite of reproducible incidents defined by a network scenario and an injectable root cause. The full working matrix contains 15 scenarios, 59 fault types, and 702 cases.
2. **NIKA Orchestrator** — a modular platform that deploys live labs, injects faults, connects agents to interactive MCP tools, and evaluates their submissions.

### Network incidents

Network incidents in the NIKA's benchmark is constructed starting from recurring root cause failures, drawn from the following categories: 

| Category | Fault types | # Cases |
| --- | :---: | :---: |
| End-host failures | 9 | 161 |
| Link failures | 7 | 129 |
| Misconfigurations | 11 | 169 |
| Network node errors | 11 | 65 |
| Network under attack | 6 | 59 |
| Resource contention | 8 | 119 |
| **Total** | **59** | **702** |

<details>
<summary>🔍 <strong>Show all 59 failure types</strong></summary>

NIKA's benchmark consists of troubleshooting tasks based on incidents that are recurring in production networks. Each incident is defined by its root cause `Problem ID`, and a network scenario where the root cause is injected (e.g., network topology and traffic workload). Combining the root cause + network scenario dimensions, NIKA's benchmark currently contains a total of 702 unique tasks. The table below lists the root cause problems currently injected, and the number of tasks in the benchmark where they appear.


| Problem ID | Category | Description | # Tasks |
| ---------- | -------- | ----------- | ------- |
| `dns_record_error` | `end_host_failure` | Some hosts cannot access external websites. | 6 |
| `host_crash` | `end_host_failure` | host_crash | 28 |
| `host_incorrect_dns` | `end_host_failure` | Some hosts are unable to access web services. | 6 |
| `host_incorrect_gateway` | `end_host_failure` | Some hosts seem to be unreachable in the network. | 17 |
| `host_incorrect_ip` | `end_host_failure` | Some hosts seem to be unreachable in the network. | 28 |
| `host_incorrect_netmask` | `end_host_failure` | Some hosts seem to be unreachable in the network. | 17 |
| `host_ip_conflict` | `end_host_failure` | Some hosts experience intermittent connectivity issues. | 28 |
| `host_missing_ip` | `end_host_failure` | Some hosts are unable to communicate with other devices in the network. | 28 |
| `host_vpn_membership_missing` | `end_host_failure` | host_vpn_membership_missing | 3 |
| `bmv2_switch_down` | `link_failure` | bmv2_switch_down | 4 |
| `dhcp_service_down` | `link_failure` | dhcp_service_down | 3 |
| `dns_service_down` | `link_failure` | Some hosts cannot access external websites. | 6 |
| `link_detach` | `link_failure` | Users report connectivity issues to other hosts. | 29 |
| `link_down` | `link_failure` | Users report connectivity issues to other hosts. | 29 |
| `link_flap` | `link_failure` | Users report connectivity issues to other hosts. | 29 |
| `link_fragmentation_disabled` | `link_failure` | Users report partial packet loss when communicating with other hosts. | 29 |
| `arp_acl_block` | `misconfiguration` | arp_acl_block | 28 |
| `bgp_acl_block` | `misconfiguration` | bgp_acl_block | 9 |
| `bgp_asn_misconfig` | `misconfiguration` | Some hosts are experiencing connectivity issues. | 9 |
| `bgp_blackhole_route_leak` | `misconfiguration` | bgp_blackhole_route_leak | 9 |
| `bgp_missing_route_advertisement` | `misconfiguration` | bgp_missing_route_advertisement | 9 |
| `dhcp_missing_subnet` | `misconfiguration` | dhcp_missing_subnet | 3 |
| `dns_port_blocked` | `misconfiguration` | dns_port_blocked | 6 |
| `host_static_blackhole` | `misconfiguration` | host_static_blackhole | 9 |
| `http_acl_block` | `misconfiguration` | http_acl_block | 13 |
| `icmp_acl_block` | `misconfiguration` | icmp_acl_block | 28 |
| `k8s_coredns_isolated` | `misconfiguration` | Applications cannot resolve Kubernetes service names such as *.svc.cluster.local and report DNS timeouts, while communication by IP address keeps working. The CoreDNS pods are Running and Ready and the DNS Service still lists its endpoints. | 0 |
| `k8s_worker_apiserver_partition` | `misconfiguration` | One Kubernetes worker node reports NotReady and stops receiving new pods, and `kubectl exec` / `kubectl logs` time out for the pods it hosts, while those pods keep serving traffic and the node itself is still reachable over the network. | 0 |
| `mac_address_conflict` | `misconfiguration` | mac_address_conflict | 28 |
| `ospf_acl_block` | `misconfiguration` | ospf_acl_block | 6 |
| `ospf_area_misconfiguration` | `misconfiguration` | ospf_area_misconfiguration | 6 |
| `ospf_neighbor_missing` | `misconfiguration` | ospf_neighbor_missing | 6 |
| `flow_rule_loop` | `network_node_error` | flow_rule_loop | 6 |
| `flow_rule_shadowing` | `network_node_error` | flow_rule_shadowing | 6 |
| `frr_service_down` | `network_node_error` | Users report connectivity issues to other hosts in the network. | 17 |
| `k8s_clusterip_routing_broken` | `network_node_error` | Pods scheduled on one Kubernetes node cannot reach any ClusterIP Service, including in-cluster DNS, while direct pod-IP traffic from the same node still works. Services, endpoints and pods all report healthy, and the node stays Ready. | 0 |
| `mpls_label_limit_exceeded` | `network_node_error` | mpls_label_limit_exceeded | 1 |
| `p4_aggressive_detection_thresholds` | `network_node_error` | p4_aggressive_detection_thresholds | 1 |
| `p4_compilation_error_parser_state` | `network_node_error` | p4_compilation_error_parser_state | 4 |
| `p4_header_definition_error` | `network_node_error` | p4_header_definition_error | 4 |
| `p4_table_entry_misconfig` | `network_node_error` | p4_table_entry_misconfig | 4 |
| `p4_table_entry_missing` | `network_node_error` | p4_table_entry_missing | 4 |
| `sdn_controller_crash` | `network_node_error` | sdn_controller_crash | 6 |
| `southbound_port_block` | `network_node_error` | southbound_port_block | 6 |
| `southbound_port_mismatch` | `network_node_error` | southbound_port_mismatch | 6 |
| `arp_cache_poisoning` | `network_under_attack` | arp_cache_poisoning | 28 |
| `bgp_hijacking` | `network_under_attack` | bgp_hijacking | 9 |
| `dhcp_spoofed_dns` | `network_under_attack` | Some hosts can not access webservices. | 3 |
| `dhcp_spoofed_gateway` | `network_under_attack` | dhcp_spoofed_gateway | 3 |
| `dhcp_spoofed_subnet` | `network_under_attack` | dhcp_spoofed_subnet | 3 |
| `web_dos_attack` | `network_under_attack` | Users reports high latency when accessing some web services. | 13 |
| `dns_lookup_latency` | `resource_contention` | Users experience high latency when accessing web services. | 6 |
| `incast_traffic_network_limitation` | `resource_contention` | incast_traffic_network_limitation | 13 |
| `link_bandwidth_throttling` | `resource_contention` | link_bandwidth_throttling | 29 |
| `link_high_packet_corruption` | `resource_contention` | link_high_packet_corruption | 29 |
| `load_balancer_overload` | `resource_contention` | load_balancer_overload | 3 |
| `receiver_resource_contention` | `resource_contention` | receiver_resource_contention | 13 |
| `sender_application_delay` | `resource_contention` | sender_application_delay | 13 |
| `sender_resource_contention` | `resource_contention` | sender_resource_contention | 13 |
| - | **Total** | - | **702** |

</details>


## ✨ Features

- **Network emulators**: NIKA attaches to state-of-the-art network emulators as backends. Are you a [Kathará](https://www.kathara.org) or [Containerlab](https://containerlab.dev) user? You can use NIKA with both.
- **Pre-built incident scenarios**: Running your evals is quite simple: start any of the pre-built network scenarios in the NIKA benchmark, with automatic incident replay and evaluation mechanisms.
- **Bring any AI agent**: You can use our default agents (Claude Code, Codex, LangGraph), or plug your custom AI agent harness, see [Agent integration workflow](docs/custom-agents.md).
- **Agent sandboxing**: Agents run in isolated environments, with controlled access to the network, filesystem and telemetry tools, see [Agent sandboxing](docs/agent-sandbox.md).
- **YAML-based fault injection**: Failures can be customized via a declarative interface: `nika failure describe`, and later `--set key=value`.
- **MCP network telemetry**: Pingmesh server, InfluxDB network telemetry and CLI access to routers and switches.
- **Multi-session**: Run isolated sessions in parallel to speed up your evaluations.
- **Remote execution mode**: Run the emulated network and telemetry MCP gateways on any remote server, see [NIKA Remote](docs/remote.md).
- **Reproducibility and leaderboard**: Refer to the frozen `nika-bench` releases, and submit your results to our up-to-date leaderboard.
- **NIKA SDK**: For users who wish to extend with new failure cases using NIKA's APIs for traffic generation and fault injection, see [Creating benchmark tasks](docs/creating-benchmark-tasks.md).


## 📦 Installation

**Requirements**: Python 3.12+, and [uv](https://docs.astral.sh/uv/) for dependency management. 
Additionally, NIKA needs Docker and at least one network emulation backend. Currently supported backends are:

- **[Kathará](https://www.kathara.org/)** — install with `--extra kathara` option below.
- **[Containerlab](https://containerlab.dev/)** — install with `--extra containerlab` option below.
- **Both** — install with `--extra labs` option below.

### Basic setup

```shell
git clone https://github.com/sands-lab/nika
cd nika
uv sync --extra labs   # or --extra kathara / --extra containerlab / (no extra)
source .venv/bin/activate
cp .env.example .env
```

### API keys and credentials

- Add the model credentials and agent settings you need to `.env`. 
- CLI flags override `.env` values. 

### Remote Deployments:

- **Agent Sandboxing**: See the [agent sandbox guide](docs/agent-sandbox.md) for sandboxed execution requirements.
- **Remote Mode**: This is useful if you do not want to install an emulation backend on your machine, or/and if you want to scale the emulated network to many virtual nodes on a high-capacity server, while running the agent locally. Please follow [Remote Agent Mode](docs/remote.md) documentation for more details.

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

Benchmark runs deploy the lab, inject the fault, run the selected agent, close the session, and compute rule-based metrics. LLM judging is an optional post-processing step to analyse the rollout via an independent LLM-based critic.

## 📖 Learn more

Pick the path that matches what you're trying to do:

**🏁 I want to run the benchmark, any agent**

1. [Quick start](#-quick-start) — deploy a lab, inject a fault, run an agent, evaluate.
2. [CLI reference](src/nika/cli/README.md) — `nika` commands, sessions, and result paths.
3. [Leaderboard submission](docs/leaderboard-submission.md)

**🔌 I want to connect my own agent**

1. [Built-in agents](src/agent/README.md) — built-in agents and configuration, for reference implementations.
2. [Agent integration workflow](docs/custom-agents.md) — agent contract and integration workflow.
3. [Agent skills](docs/agent-skills.md) — reusable troubleshooting knowledge you can attach to an agent.
3. [Agent sandboxing](docs/agent-sandbox.md) — isolated microVM execution.

**🌐 I want to create a new network scenario**

1. [Creating benchmark tasks](docs/creating-benchmark-tasks.md)
2. [Network incidents](docs/failure-types.md)
3. [Testing guide](tests/README.md)


## Network management benchmarks

NIKA is part of a growing ecosystem. The table below compares NIKA with other benchmarks in terms of their focus, agent interactivity, variety, scale, and realism. While the best benchmark depends on your use case, we believe that **NIKA is currently the most comprehensive online benchmark for realistic agentic evaluations**.

| Benchmark | Description | Variety | Scale | Environment Realism | Type | Best for |
|---|---|:---:|:---:|:---:|:---:|---|
| **[NIKA]((https://sands-lab.github.io/nika))** | Live network troubleshooting | ⭐️⭐️⭐️ <br> 59 fault types <br> 6 networks types | ⭐️⭐️ <br> ~700 incident variants | ⭐️⭐️⭐️ <br> ✔ Kathará/Containerlab emulation <br> ✔ Vendor CLIs & telemetry tools | 🟢 Online | Agentic evals |
| [NetOpsBench](https://github.com/NetX-lab/NetOpsBench) | Live network troubleshooting | ⭐️ <br> 13 fault types <br> 1 network type | ⭐️⭐️ <br>~600 incident variants | ⭐️⭐️⭐️ <br> ✔ Containerlab emulation <br> ✔ Vendor CLIs & telemetry tools | 🟢 Online | Agentic evals |
| [NetArena](https://github.com/Froot-NetSys/NetArena) | Network operations | ⭐️ <br> 3 setups, 5 fault types | ⭐️⭐️⭐️ <br> ~9,000 variants | ⭐️⭐️ <br>Mininet <br> Basic netutils (e.g., ping) | 🟢 Online | Large-scale synthetic variants for ML |
| [NetConfEval](https://github.com/RedHatResearch/conext24-NetConfEval) | Basic network configuration | ⭐️ <br> Reachability, waypoint, load balancing on 8x topologies | ⭐️⭐️⭐️ <br> ~3,000 variants | ⭐️ <br> Simple offline validator | 🔴 Offline / Static | Basic LLM config-generation capability |
| [Cornetto](https://arxiv.org/abs/2604.22513) | Config-repair with formal verification | ⭐️⭐️ <br> 50 fault types | ⭐️⭐️ <br> 231 problems | ⭐️ <br> Batfish | 🔴 Offline / Static | LLM config repair |
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
