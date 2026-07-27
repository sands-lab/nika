<div align="center">

<img src="./assets/images/nika-banner.svg" alt="NIKA" width="100%"/>

<br />

[🤖Overview](#🤖overview) | 
[📦Installation](#📦installation) | 
[🚀Quick Start](#🚀quick-start) | 
[🛠️Usage](#🛠️usage) | 
[🌐 Website](https://sands-lab.github.io/nika/) | 
[📚Cite](#📚cite)

[![ArXiv Link](https://img.shields.io/badge/arXiv-2512.16381-red?logo=arxiv)](https://arxiv.org/abs/2512.16381) 
[![Project Page](https://img.shields.io/badge/-Project%20Page-1E88E5?logo=googlechrome&logoColor=white&labelColor=24292f)](https://sands-lab.github.io/nika/)
[![Open Telco AI](https://img.shields.io/badge/-Open%20Telco%20AI-00AEEF?logo=gsma&logoColor=white&labelColor=24292f)](https://www.open-telco.ai/resources/nika/)

</div>

## What is NIKA?

Think about [SWE-Bench](https://github.com/swe-bench/SWE-bench), but for network troubleshooting. [NIKA](https://sands-lab.github.io/nika/), **N**etwork **I**ncident Benchmar**k** for **A**I Agents,
is an *open benchmark for agentic evals on network troubleshooting tasks*. It connects any agent directly to a live network stack: routers, switches, hosts, and telemetry tools, all running on general-purpose compute. NIKA reproduces hundreds of realistic faults covering data center networks, campus networks, ISP backbones, SDN fabrics, overlay networks, and Kubernetes CNIs. 


## 🙋 Why NIKA?

NIKA lets you plug in any LLM or agent framework and measure its operational capability under identical, reproducible conditions. 

It helps different users answer questions like:

- 💬 **Network Manager** "A vendor is pitching me an AI solution for network operations. It passes all the standard telecom benchmarks (TeleQnA, TeleLogs, TeleMath, 3GPP-TSG), but I need objective evidence it can handle real incidents before I sign off."
- 💬 **Network Engineer and SREs** "I respond to network incidents every day. I want an AI agent to help, but I'm not sure it will understand my topology or make things worse."
- 💬 **AI Researcher** "I'm designing a new agent architecture for long-horizon network tasks. I need a benchmark to ablate components, measure reproducibly, and compare against published baselines."
- 💬 **AI/ML Engineer** "I want to fine-tune an open-source model on network troubleshooting and need a structured dataset paired with a rigorous evaluation framework."
- 💬 **Open-Source Contributor** "I want to contribute a new network scenario or fault type to the community and have it evaluated systematically."

<h1 id="🤖overview">🤖 Overview</h1>

![NIKA Architecture](./assets/images/architecture.png)

NIKA is a unified platform that combines: 
1. **NIKA Benchmark**: A benchmark suite of curated network incidents. Incidents are uniquely defined by their underlying [root-cause issue (I)](#network-issues) and [network scenario (N)](#network-scenarios). NIKA currently covers 56 network issues, including soft-, hard-, and gray-failures, and is shipped with 15 pre-defined network scenarios spanning campus, data center, and cloud-native networks. The full benchmark YAML currently yields 702 troubleshooting incidents for evaluating AI agents.
2. **NIKA Orchestrator**: A modular plug-and-play orchestration platform that connects AI agents with the *network environment*, enabling real-time access to telemetry interfaces via MCP telemetry severs, and providing a human-facing interface to judge agent performance. The orchestrator materializes the network incidents into the network environment starting from the incident specs. 

## Features

- **Network emulators**: NIKA attaches to state-of-the-art network emulators as backends. Are you a [Kathará](https://www.kathara.org) or [containerlab](https://containerlab.dev) user? you can use NIKA with both.
- **Fault injection**: Parameterized fault injection (`nika failure describe`, `--set key=value`)
- **Bring any AI agent**: Easy integration of custom AI agent.
- **Agent sandbox**: CLI / SDK / SADE agents run in Docker Sandboxes (`sbx` microVMs with official `codex` / `claude` / `shell` templates), isolated per session (workspace + MCP gateway port). Codex uses OpenAI; Claude/SADE use DeepSeek Anthropic-compatible API keys by default.
- **Zero-touch eval**: Pre-built network scenarios and fault injection mechanisms, with automatic evaluation mechanism.
- **MCP**: native MCP-based tool support.
- **Reproducibility**: Frozen `nika-bench@0.1.0` release with content digests, git commit stamping, and batch summary (`nika eval summary`).
- **CLI**: Unified `nika` CLI for env deploy, fault injection, agent runs, and evaluation
- **Multi-session evals**: Session-based workflow with multi-session support (`nika session`, `--session_id`). Run isolated sessions in parallel to speed-up evaluations.
- **NIKA SDK**: Extend with your own network topology and configuration, and reproduce your failure case using NIKA's modules for traffic generation and fault injection. 

<h1 id="📦installation">📦 Installation</h1>

## Requirements

- [Kathará](https://www.kathara.org/). 
  Follow the [official installation guide](https://github.com/KatharaFramework/Kathara?tab=readme-ov-file#installation) to install Kathará. Required for Kathará-backed scenarios.
- [Containerlab](https://containerlab.dev/). Required only for Containerlab-backed scenarios.
- Python >= 3.12


## Setup

Clone the repository and install the dependencies. 
NIKA uses [uv](https://docs.astral.sh/uv) to manage the dependencies. Follow [uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/) to install uv. You can also use a standard `pip install -e .` to install the dependencies.

```shell
# Clone the repository
git clone https://github.com/sands-lab/nika
cd nika

# Install dependencies
uv sync

# Activate the environment
source .venv/bin/activate
```

The Kathará API relies on Docker to function properly. We recommend to add current user to docker group to avoid calling with `sudo`. **However, please be aware of the security implications of this action.**

```shell
sudo usermod -aG docker $USER
```

Login again or activate temporaily with 

```shell
newgrp docker
```

<br />
<h1 id="🚀quick-start">🚀 Quick Start</h1>

## Configure environment variables

Copy the template and fill in values. **NIKA does not ship hard-coded agent defaults** — every `nika agent run` / `nika benchmark run` needs either a configured `.env` or explicit CLI flags.

```shell
cp .env.example .env
```

CLI flags override `.env` when both are set.

### Agent and judge settings

Shared flags (all agents): `-a` / `NIKA_AGENT_TYPE`, `-n` / `NIKA_MAX_STEPS`, `-m` / `NIKA_MODEL` (optional override). Per-agent env vars and examples are in **[Troubleshooting Agents](#troubleshooting-agents)**.

| Setting | `.env` variable | CLI flag |
|---------|-----------------|----------|
| Results parent dir | `NIKA_RESULT_DIR` | `--result_dir` on `nika env run`, `nika benchmark run` |
| Judge provider | `NIKA_JUDGE_PROVIDER` | `-p` on `nika eval judge` |
| Judge model | `NIKA_JUDGE_MODEL` | `-m` on `nika eval judge` |

API keys and observability: see [`.env.example`](.env.example).

## Step by step guide
You can follow the steps below to run a complete troubleshooting task with NIKA. Use the `nika` CLI.

Each `nika env run` creates a **session** (printed as `session_id=…`). Session state lives under `runtime/sessions/` and tracks the deployed lab, injected failures, and agent activity. When only one session is running, most commands auto-select it; pass `--session_id` when several sessions are active.

1. **List scenarios and start the network environment**

   ```shell
   nika env list
   nika env run <scenario>                    # scenarios without topology sizes (e.g. simple_bgp)
   nika env run <scenario> -s s             # scalable scenarios (size: s, m, or l)
   nika env ps                                # running lab instances (grouped by deployed env)
   ```

2. **Inspect and manage sessions**

   ```shell
   nika session ps                            # running sessions (status, failures, agents)
   nika session ps -a                         # include finished sessions
   nika session inspect [--session_id ID]       # full session JSON + failure summary
   nika session inspect -c                    # also list lab containers (docker-ps style)
   nika session containers [--session_id ID]  # list containers in the session lab
   nika session close [--session_id ID]       # undeploy lab and clear runtime state
   nika session wipe -y                       # close every running session and wipe Kathara/Containerlab
   ```

3. **List problems and inject faults**

   ```shell
   nika failure list
   nika failure describe <problem_id>         # required parameter schema
   nika failure inject <problem_id> --set host_name=pc1 --set intf_name=eth0
   nika failure ps [--session_id ID]          # persisted injection records
   ```

4. **Run commands inside a lab host** (optional debugging)

   ```shell
   nika exec pc1 ip addr show
   nika exec pc1 ping -c 3 10.0.0.2 --timeout 30
   ```

5. **List agent options and run the agent**

   ```shell
   nika agent list
   nika agent run -a byo.langgraph -p openai -m gpt-5-mini -n 20
   nika agent run -a byo.mcp_agent -m gpt-4.1-mini -n 20
   nika agent run -a byo.autogen -m gpt-4.1-mini -n 20
   nika agent run -a local_cli.codex_cli -m gpt-5.4-mini
   nika agent run -a local_cli.claude_cli
   nika agent run -a community.sade -n 20
   ```

   See **[Troubleshooting Agents](#troubleshooting-agents)** for per-agent configuration.

6. **Close the session, then evaluate the run** (metrics, judge, and CSV summary are separate steps)

   ```shell
   nika session close [--session_id ID] -y    # undeploy lab and clear runtime state first
   nika eval metrics
   nika eval judge -p openai -m gpt-5-mini
   nika eval summary                              # all finished sessions → default CSV
   nika eval summary -p link_down -e simple_bgp   # filter by problem and scenario
   nika eval summary -o results/0_summary/my_run.csv
   nika eval clean -y                              # wipe results/, session JSON, and SQLite index
   ```

Full CLI documentation (traffic types, parameter tables, and conventions) lives in **[src/nika/cli/README.md](src/nika/cli/README.md)**. Developer guides: **[Creating Benchmark Tasks](docs/creating-benchmark-tasks.md)** (scenarios, `ProblemBase` faults, benchmark YAML), **[Custom Agents](docs/custom-agents.md)**, **[Agent Skills](docs/agent-skills.md)**, and **[Leaderboard Submission](docs/leaderboard-submission.md)**.

## Benchmark

`nika benchmark run` runs the experiment pipeline: deploy lab → inject fault → run agent → close session → rule-based metrics. **LLM judge and CSV summary belong to `nika eval`**. You must pass **`--release`** (frozen suite) or **`--config`** (ad-hoc YAML); there is no bare default. Details: **[benchmark/README.md](benchmark/README.md)**.

Shipped datasets:

| Path | Role |
|------|------|
| `benchmark/releases/0.1.0/` | Frozen release (`RELEASE.yaml` + Dev/Test case files) |
| `benchmark/benchmark_selected.yaml` | Editable curated suite (source for Dev freeze) |
| `benchmark/benchmark_full.yaml` | Full scenario × failure matrix (702 cases; Test source pool) |

```shell
# Frozen release (required: --release)
nika benchmark run --release 0.1.0
nika benchmark run --release 0.1.0 --result_dir results/my-run
nika benchmark releases   # list + preflight-verify

# Ad-hoc / working YAML (required: --config)
nika benchmark run --config benchmark/benchmark_selected.yaml
nika benchmark run --config benchmark/benchmark_full.yaml

# Single case (no YAML / release)
nika benchmark run dc_clos_bgp --problem bgp_asn_misconfig -s s

# Post-hoc judge + summary
nika eval judge -p openai -m gpt-5-mini --result_dir results/my_run
nika eval summary --result_dir results/my_run
```

Release runs treat `--result_dir` as one run: `run.json` (plus legacy `benchmark_job.json`) records release version/digest, `split`, case count / `cases_sha256`, agent/model/`n_trials`, NIKA git commit, and scoring; trials live under `trials/{case_key}__tNN/`. Trial count comes from `RELEASE.yaml` `defaults.n_trials` (3 for `0.1.0`).

### Leaderboard submission

After an official release run, pack, validate, and open a PR on
[`sands-lab/nika-leaderboard`](https://github.com/sands-lab/nika-leaderboard).
Guide: **[docs/leaderboard-submission.md](docs/leaderboard-submission.md)**.

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard pack --result_dir results/my-run \
  --submission results/my-run/submission
nika leaderboard validate results/my-run/YYYYMMDD_slug \
  --source-result-dir results/my-run
nika leaderboard submit results/my-run/YYYYMMDD_slug   # requires authenticated gh
```

### Custom datasets (`--config`)

Point `--config` at your own YAML to run a custom case list. Each row uses the same fields as the shipped files: `scenario`, `problem`, `topo_size` (`s`/`m`/`l`, or null for fixed-size labs), and an `inject` map passed to `nika failure inject`. Authoring and regeneration: **[Creating Benchmark Tasks](docs/creating-benchmark-tasks.md)**, `uv run python benchmark/generate_benchmark.py`.

```yaml
cases:
  - scenario: simple_bgp
    topo_size: null
    problem: link_down
    inject:
      host_name: pc1
      intf_name: eth0
```

```shell
nika benchmark run --config benchmark/my_cases.yaml --result_dir results/my_cases
```

### Result directories and resume

Use **`--result_dir`** (or `NIKA_RESULT_DIR`) to isolate runs by dataset, model, or agent. Artifacts land under `{result_dir}/{session_id}/`. Resume and skip logic scan **only** that directory.

| Flag | Behavior |
|------|----------|
| `--result_dir PATH` | Parent for session outputs (default `results/`) |
| `--resume` | **Default.** Skip finished cases (matching `benchmark_fingerprint` in `run.json`); clean incomplete sessions and re-run the rest |
| `--no-resume` | Re-run every YAML row regardless of existing artifacts |
| `--batch-size N` | Run up to `N` cases in parallel per batch |

```shell
# Isolate one experiment; resume continues after interrupt
nika benchmark run --release 0.1.0 --result_dir results/list1 --batch-size 4

# Same command again → skips completed trials in results/list1 only
nika benchmark run --release 0.1.0 --result_dir results/list1 --batch-size 4

# Force a full re-run in that directory
nika benchmark run --release 0.1.0 --result_dir results/list1 --no-resume

# Aggregate finished sessions under that directory
nika eval summary --result_dir results/list1
```

More flags and YAML field details: **[src/nika/cli/README.md](src/nika/cli/README.md)** (`nika benchmark`).

### Traffic (optional)

```shell
nika traffic list
nika traffic run od --all-to-host pc1 --mbps 20 --interval 300 --background
```

## Run Unit Tests

```shell
# run all unit tests
uv run --with pytest pytest

# verbose output
uv run --with pytest pytest -v

# run only selected test files
uv run --with pytest pytest tests/nika/runtime/ -v
uv run pytest tests/benchmark/test_resume.py -v
```

## Agent Sandbox

Non-BYO production agents (`local_cli.*`, `sdk.*`, `community.sade`) run inside **[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/)** (`sbx` microVMs) using official `codex` / `claude` / `shell` templates. The Kathara/Containerlab lab and MCP gateway stay on the host. `byo.*` agents run on the host. Concurrent runs get a per-session microVM, ephemeral workspace, and MCP gateway port (network policy blocks peer ports). Full setup: [docs/agent-sandbox.md](docs/agent-sandbox.md). Sandbox test commands and verification status: [tests/README.md](tests/README.md).

```
Host (NIKA)                         sbx microVM
├── Kathara lab + MCP gateway       ├── `codex` / `claude` template     (CLI)
├── Host phase / SDK orchestration  └── `shell` (+ optional offline wheels) (SDK/SADE)
├── sbx secret store (real keys)        placeholders only in the VM
└── results/{session_id}/               workspace: .sandbox_run/
    (one ephemeral gateway port/session; sbx policy allows only that port)
```

| Agent | sbx template | Credentials |
|-------|--------------|-------------|
| `local_cli.codex_cli` / `sdk.codex_sdk` | `codex` / `shell` | `OPENAI_API_KEY` → `openai` sbx secret, or `sbx secret set -g openai --oauth` |
| `local_cli.claude_cli` / `sdk.claude_sdk` / `community.sade` | `claude` / `shell` | `DEEPSEEK_API_KEY` or `ANTHROPIC_AUTH_TOKEN` (+ Anthropic-compatible `ANTHROPIC_BASE_URL`), or Claude `/login` |

**Prerequisites:** `sbx login`, KVM, Docker. Optional outbound proxy: `NIKA_SANDBOX_UPSTREAM_PROXY` (see `.env.example`). SDK/SADE extras: `uv sync --extra sdk --prerelease=allow`, `uv sync --extra sade`.

<h1 id="🛠️usage">🛠️ Usage</h1>

## Troubleshooting Agents

Agent implementations live under [`src/agent/`](src/agent/). All agents share the same contract (`async run(task_description) -> dict`), the same two-phase pipeline (**diagnosis** → **submission**), and write `results/{session_id}/messages.jsonl` plus `submission.json`. Extension details: **[Custom Agents](docs/custom-agents.md)** and **[src/agent/README.md](src/agent/README.md)**.

### Layout

```
src/agent/
├── byo/                  # Bring-your-own LLM / agent framework
│   ├── langgraph/        # -a byo.langgraph
│   ├── mcp_agent/        # -a byo.mcp_agent
│   └── autogen/          # -a byo.autogen
├── local_cli/            # Local CLI subprocess workers
│   ├── codex_cli/        # -a local_cli.codex_cli
│   └── claude_cli/       # -a local_cli.claude_cli
├── community/            # Community-contributed agents
│   └── sade/             # -a community.sade
└── sdk/                  # SDK agents
    ├── claude_sdk/       # -a sdk.claude_sdk
    └── codex_sdk/        # -a sdk.codex_sdk
```

| CLI name | Package | Orchestration | LLM access |
| -------- | ------- | ------------- | ---------- |
| `byo.langgraph` | `byo/langgraph` | LangGraph `StateGraph` | LangChain ReAct + `load_model()` |
| `byo.mcp_agent` | `byo/mcp_agent` | mcp-agent `Workflow` | [mcp-agent SDK](https://docs.mcp-agent.com/mcp-agent-sdk/overview) + OpenAI |
| `byo.autogen` | `byo/autogen` | AutoGen `GraphFlow` | [AutoGen AgentChat](https://microsoft.github.io/autogen/stable/) + OpenAI |
| `local_cli.codex_cli` | `local_cli/codex_cli` | Native two-phase (no LangGraph) | `codex exec` subprocess + shared skills |
| `local_cli.claude_cli` | `local_cli/claude_cli` | Native two-phase (no LangGraph) | `claude -p` subprocess + shared skills |
| `community.sade` | `community/sade` | Single Claude Code session + skill library | `claude-agent-sdk` (optional extra `sade`) |
| `sdk.claude_sdk` | `sdk/claude_sdk` | Native two-phase `ClaudeSDKClient` | `claude-agent-sdk` + shared skills (optional extra `sdk`) |
| `sdk.codex_sdk` | `sdk/codex_sdk` | Native two-phase `AsyncCodex` | `openai-codex` + shared skills (optional extra `sdk`) |

### Shared configuration

| Flag | Env | Notes |
|------|-----|-------|
| `-a` / `--agent` | `NIKA_AGENT_TYPE` | Required |
| `-n` / `--max-steps` | `NIKA_MAX_STEPS` | Per-phase step limit (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, `sdk.claude_sdk`) |
| `-m` / `--model` | `NIKA_MODEL` | Overrides agent-specific model env when set |
| `--session_id` | — | Target session (default: current running session) |

Model resolution: `-m` → `NIKA_MODEL` → agent-specific env (below).

### Agent skills

Claude Code and Codex agents load reusable skill libraries during diagnosis when `NIKA_ENABLE_SKILLS=true` (default). The shared library lives under [`src/agent/skills/`](src/agent/skills/). Override with `NIKA_SKILLS_DIR`.

- **Claude agents** (`local_cli.claude_cli`, `sdk.claude_sdk`): native `Skill(skill="...")` tool + `.claude/skills/`
- **Codex agents** (`local_cli.codex_cli`, `sdk.codex_sdk`): `.agents/skills/` + workspace `AGENTS.md`
- **SADE** (`community.sade`): separate 15-skill library under `src/agent/community/sade/.claude/`

Authoring guide: **[docs/agent-skills.md](docs/agent-skills.md)**.

### `byo.langgraph` (`byo/langgraph`)

LangGraph + LangChain ReAct workers per phase. Requires `-p` / `NIKA_LLM_PROVIDER` (`openai`, `deepseek`, `ollama`, `custom`).

| Provider | Credential |
|----------|------------|
| `openai` | `OPENAI_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `ollama` | `OLLAMA_API_URL` (default `http://localhost:11434`) |
| `custom` | `CUSTOM_API_BASE`, optional `CUSTOM_API_KEY` |

| Env | Default |
|-----|---------|
| `NIKA_LANGGRAPH_MODEL` | `gpt-5-mini` |

```shell
nika agent run -a byo.langgraph -p openai -m gpt-5-mini -n 20
nika agent run -a byo.langgraph -p ollama -m qwen2.5:7b -n 20
```

Observability: optional Langfuse (`byo.langgraph` only, enable with `NIKA_LANGFUSE_ENABLED=true`). See `.env.example`.

### `byo.mcp_agent` (`byo/mcp_agent`)

[mcp-agent SDK](https://docs.mcp-agent.com/mcp-agent-sdk/overview) workers per phase. Orchestration uses mcp-agent ``Workflow``.

| Env | Default |
|-----|---------|
| `NIKA_MCP_AGENT_MODEL` | `gpt-4.1-mini` |

```shell
nika agent run -a byo.mcp_agent -m gpt-4.1-mini -n 20
```

### `byo.autogen` (`byo/autogen`)

[AutoGen AgentChat](https://microsoft.github.io/autogen/stable/) ``GraphFlow`` workers per phase. Each phase uses an `AssistantAgent` with MCP tools from the same Kathara / task MCP servers as other agents.

| Env | Default |
|-----|---------|
| `NIKA_AUTOGEN_MODEL` | `gpt-4.1-mini` |

```shell
nika agent run -a byo.autogen -m gpt-4.1-mini -n 20
```

### `local_cli.codex_cli` (`local_cli/codex_cli`)

Requires [Codex CLI](https://developers.openai.com/codex) on `PATH`. Runs inside Docker Sandboxes (`sbx` `codex` template). Ephemeral agent workspace is discarded after the run; session results match other agents (`messages.jsonl`, `submission.json`). Loads shared skills from `src/agent/skills/` when `NIKA_ENABLE_SKILLS=true`.

Auth: `OPENAI_API_KEY` → sbx `openai` secret, or `sbx secret set -g openai --oauth`.

| Flag | Env | Notes |
|------|-----|-------|
| `-m` / `--model` | `NIKA_CODEX_MODEL` | Default `gpt-5.4-mini` |
| `-e` / `--reasoning-effort` | `NIKA_CODEX_REASONING_EFFORT` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |

```shell
nika agent run -a local_cli.codex_cli -m gpt-5-mini -e medium
```

### `local_cli.claude_cli` (`local_cli/claude_cli`)

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) on `PATH`. Runs inside Docker Sandboxes (`sbx` `claude` template). Ephemeral agent workspace is discarded after the run; session results match other agents (`messages.jsonl`, `submission.json`). Loads shared skills via `--setting-sources project` when `NIKA_ENABLE_SKILLS=true`.

Auth (pick one): `DEEPSEEK_API_KEY` / `ANTHROPIC_AUTH_TOKEN` (+ `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`), native `ANTHROPIC_API_KEY`, or Claude `/login` subscription on the host.

Model when `-m` omitted (first non-empty): `ANTHROPIC_MODEL` → `CLAUDE_CODE_SUBAGENT_MODEL` → `ANTHROPIC_DEFAULT_SONNET_MODEL`.

```shell
nika agent run -a local_cli.claude_cli
nika agent run -a local_cli.claude_cli -m deepseek-v4-flash
```

### `sdk.claude_sdk` (`sdk/claude_sdk`)

Native two-phase pipeline via `claude-agent-sdk` `ClaudeSDKClient` sessions (no LangGraph). Requires `uv sync --extra sdk --prerelease=allow`. Loads shared skills when `NIKA_ENABLE_SKILLS=true`. Runs inside Docker Sandboxes (`sbx` `shell`; optional offline wheels via `NIKA_SANDBOX_OFFLINE_SDK_WHEELS`).

Auth: DeepSeek or Anthropic via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` / `DEEPSEEK_API_KEY` (same as `local_cli.claude_cli`).

| Flag | Env | Notes |
|------|-----|-------|
| `-n` / `--max-steps` | `NIKA_MAX_STEPS` | SDK `max_turns` per phase |
| `-m` / `--model` | `NIKA_CLAUDE_SDK_MODEL` or `ANTHROPIC_MODEL` chain | |

```shell
nika agent run -a sdk.claude_sdk -n 20
nika agent run -a sdk.claude_sdk -m deepseek-v4-flash
```

### `sdk.codex_sdk` (`sdk/codex_sdk`)

Native two-phase pipeline via `openai-codex` `AsyncCodex` threads. Requires `uv sync --extra sdk --prerelease=allow`. Loads shared skills when `NIKA_ENABLE_SKILLS=true`. Runs inside Docker Sandboxes (`sbx` `shell`; optional offline wheels via `NIKA_SANDBOX_OFFLINE_SDK_WHEELS`).

Auth: `OPENAI_API_KEY` synced into the built-in `openai` sbx secret, or `sbx secret set -g openai --oauth`.

| Flag | Env | Notes |
|------|-----|-------|
| `-m` / `--model` | `NIKA_CODEX_SDK_MODEL` or `NIKA_CODEX_MODEL` | Default `gpt-5.4-mini` |
| `-e` / `--reasoning-effort` | `NIKA_CODEX_REASONING_EFFORT` | Same as `local_cli.codex_cli` |

```shell
nika agent run -a sdk.codex_sdk -m gpt-5-mini -e medium
```

### `community.sade` (`community/sade`)

Single Claude Code session with SADE's phase-gated prompt and a **15-skill fault-family library** under `src/agent/community/sade/.claude/`. Requires `uv sync --extra sade`. Auth same as `local_cli.claude_cli` (env API only).

| Env | Notes |
|-----|-------|
| `NIKA_SADE_MODEL` | Optional model override |
| `ANTHROPIC_MODEL` chain | Same as `local_cli.claude_cli` |

```shell
nika agent run -a community.sade -n 20
```

See [`src/agent/community/sade/README.md`](src/agent/community/sade/README.md) and the [SADE paper](https://arxiv.org/abs/2605.04530).

### Example: `simple_bgp` with `link_down`

End-to-end workflow from lab deploy through agent run and evaluation:

```shell
# 1. Deploy the network environment (creates a session)
nika env list
nika env run simple_bgp
# → prints session_id=20260613-061340-072e35

# 2. Inspect the fault schema, then inject a link-down on pc1
nika failure describe link_down
nika failure inject link_down --set host_name=pc1 --set intf_name=eth0

# 3. (optional) verify the fault from inside the lab
nika exec pc1 ip link show eth0
nika exec pc2 ping -c 3 195.11.14.2

# 4. Run a troubleshooting agent on the session task
nika agent run -a byo.langgraph -p openai -m gpt-5-mini -n 20
# nika agent run -a byo.mcp_agent -m gpt-4.1-mini -n 20
# nika agent run -a byo.autogen -m gpt-4.1-mini -n 20
# nika agent run -a local_cli.codex_cli -m gpt-5.4-mini

# 5. Inspect session state and artifacts
nika session inspect
ls {result_dir}/{session_id}/
# run.json, ground_truth.json, events.jsonl, messages.jsonl, submission.json

# 6. Close the lab, then evaluate
nika session close -y
nika eval metrics
nika eval judge -p openai -m gpt-5-mini
```

When multiple sessions are running, pass `--session_id <id>` to `failure inject`, `agent run`, and other session-scoped commands.

## Network Scenarios

Registered scenarios (see `nika env list`) live under `src/nika/net_env/`, organized by backend:

- `kathara/` — Kathara-based scenarios (topology generators, startup files, network configs)
- `containerlab/` — Containerlab-based scenarios

### Backend support

NIKA supports two lab backends:

- `kathara` — uses Kathará labs and Docker containers, and covers the existing routing, SDN, P4, and Kubernetes scenarios under `src/nika/net_env/kathara/`.
- `containerlab` — uses Containerlab topology files and vendor/network OS containers for scenarios under `src/nika/net_env/containerlab/`, such as `min3clos`.

Each scenario is bound to exactly one backend. Use `nika env list` to see which backend a scenario uses:

```shell
nika env list
nika env run simple_bgp
nika env run min3clos
```

Backend information is stored in the session metadata and reused by session-scoped commands such as `nika exec`, `nika failure inject`, `nika agent run`, and `nika session close`. Kathará scenarios build their topology from `lab.py`; Containerlab scenarios render topology files under `runtime/containerlab/`.

| Scenario ID | Scalable | Backend | Description |
| ----------- | -------- | -------- | ----------- |
| `dc_clos_bgp` | ✓ | kathara | Multi-tier data center CLOS with EBGP (FRR). |
| `dc_clos_service` | ✓ | kathara | Data center CLOS with DNS/HTTP edge services and external clients. |
| `ospf_enterprise_static` | ✓ | kathara | Enterprise hierarchical OSPF network with static host addressing. |
| `ospf_enterprise_dhcp` | ✓ | kathara | Enterprise OSPF network with DHCP for host addressing. |
| `rip_small_internet_vpn` | ✓ | kathara | Small RIP-based Internet with external zones and WireGuard VPN overlay. |
| `sdn_clos` | ✓ | kathara | Scalable SDN spine–leaf fabric with OpenFlow controller. |
| `sdn_star` | ✓ | kathara | SDN star (hub-and-spoke) topology with OpenFlow controller. |
| `simple_bgp` | -- | kathara | Compact inter-domain BGP lab (two routers, two hosts). |
| `p4_int` | -- | kathara | P4 spine–leaf testbed with In-band Network Telemetry (InfluxDB). |
| `p4_bloom_filter` | -- | kathara | P4 bloom-filter data-plane validation testbed. |
| `p4_counter` | -- | kathara | P4 counter pipeline testbed. |
| `p4_mpls` | -- | kathara | P4 MPLS data-plane testbed. |
| `k8s_lab` | -- | kathara | Fat-tree BGP fabric with k3s cluster, MetalLB, NGINX Ingress, and sample microservices. See [k8s_lab README](src/nika/net_env/kathara/kubernetes/k8s_lab/README.md). |
| `llmd_lab` | -- | kathara | Star topology with k3s cluster running llm-d disaggregated Prefill/Decode inference (simulated, no GPU). See [llmd_lab README](src/nika/net_env/kathara/kubernetes/llmd_lab/README.md). |
| `min3clos` | -- | containerlab | 3-node CLOS fabric with Nokia SR Linux ([Containerlab min clos](https://containerlab.dev/lab-examples/min-clos/)). |

Each scenario is registered in `src/nika/net_env/net_env_pool.py` and declares its supported backend (`kathara` or `containerlab`). See **[Creating Benchmark Tasks](docs/creating-benchmark-tasks.md)** for the NIKA extension workflow, and check [Kathará API Docs](https://github.com/KatharaFramework/Kathara/wiki/Kathara-API-Docs) or [Containerlab docs](https://containerlab.dev/) for backend details.

## Network issues

This framework provides a set of predefined issues that can be injected into the network environment. The current problem registry contains 56 root-cause ids under `src/nika/problems/`; `benchmark/benchmark_full.yaml` expands them into 702 single-fault benchmark cases across the registered scenarios. Inject parameters must be specified explicitly (see `nika failure describe`); network addresses are read from the target host at inject time.
The following table is generated from `ProblemMeta` plus case counts in `benchmark/benchmark_full.yaml`:

| Category | Problem ID | Description | # Cases |
| -------- | ---------- | ----------- | ------- |
| `end_host_failure` | `dns_record_error` | Some hosts cannot access external websites. | 6 |
| `end_host_failure` | `host_crash` | host_crash | 28 |
| `end_host_failure` | `host_incorrect_dns` | Some hosts are unable to access web services. | 6 |
| `end_host_failure` | `host_incorrect_gateway` | Some hosts seem to be unreachable in the network. | 17 |
| `end_host_failure` | `host_incorrect_ip` | Some hosts seem to be unreachable in the network. | 28 |
| `end_host_failure` | `host_incorrect_netmask` | Some hosts seem to be unreachable in the network. | 17 |
| `end_host_failure` | `host_ip_conflict` | Some hosts experience intermittent connectivity issues. | 28 |
| `end_host_failure` | `host_missing_ip` | Some hosts are unable to communicate with other devices in the network. | 28 |
| `end_host_failure` | `host_vpn_membership_missing` | host_vpn_membership_missing | 3 |
| `link_failure` | `bmv2_switch_down` | bmv2_switch_down | 4 |
| `link_failure` | `dhcp_service_down` | dhcp_service_down | 3 |
| `link_failure` | `dns_service_down` | Some hosts cannot access external websites. | 6 |
| `link_failure` | `link_detach` | Users report connectivity issues to other hosts. | 29 |
| `link_failure` | `link_down` | Users report connectivity issues to other hosts. | 29 |
| `link_failure` | `link_flap` | Users report connectivity issues to other hosts. | 29 |
| `link_failure` | `link_fragmentation_disabled` | Users report partial packet loss when communicating with other hosts. | 29 |
| `misconfiguration` | `arp_acl_block` | arp_acl_block | 28 |
| `misconfiguration` | `bgp_acl_block` | bgp_acl_block | 9 |
| `misconfiguration` | `bgp_asn_misconfig` | Some hosts are experiencing connectivity issues. | 9 |
| `misconfiguration` | `bgp_blackhole_route_leak` | bgp_blackhole_route_leak | 9 |
| `misconfiguration` | `bgp_missing_route_advertisement` | bgp_missing_route_advertisement | 9 |
| `misconfiguration` | `dhcp_missing_subnet` | dhcp_missing_subnet | 3 |
| `misconfiguration` | `dns_port_blocked` | dns_port_blocked | 6 |
| `misconfiguration` | `host_static_blackhole` | host_static_blackhole | 9 |
| `misconfiguration` | `http_acl_block` | http_acl_block | 13 |
| `misconfiguration` | `icmp_acl_block` | icmp_acl_block | 28 |
| `misconfiguration` | `mac_address_conflict` | mac_address_conflict | 28 |
| `misconfiguration` | `ospf_acl_block` | ospf_acl_block | 6 |
| `misconfiguration` | `ospf_area_misconfiguration` | ospf_area_misconfiguration | 6 |
| `misconfiguration` | `ospf_neighbor_missing` | ospf_neighbor_missing | 6 |
| `network_node_error` | `flow_rule_loop` | flow_rule_loop | 6 |
| `network_node_error` | `flow_rule_shadowing` | flow_rule_shadowing | 6 |
| `network_node_error` | `frr_service_down` | Users report connectivity issues to other hosts in the network. | 17 |
| `network_node_error` | `mpls_label_limit_exceeded` | mpls_label_limit_exceeded | 1 |
| `network_node_error` | `p4_aggressive_detection_thresholds` | p4_aggressive_detection_thresholds | 1 |
| `network_node_error` | `p4_compilation_error_parser_state` | p4_compilation_error_parser_state | 4 |
| `network_node_error` | `p4_header_definition_error` | p4_header_definition_error | 4 |
| `network_node_error` | `p4_table_entry_misconfig` | p4_table_entry_misconfig | 4 |
| `network_node_error` | `p4_table_entry_missing` | p4_table_entry_missing | 4 |
| `network_node_error` | `sdn_controller_crash` | sdn_controller_crash | 6 |
| `network_node_error` | `southbound_port_block` | southbound_port_block | 6 |
| `network_node_error` | `southbound_port_mismatch` | southbound_port_mismatch | 6 |
| `network_under_attack` | `arp_cache_poisoning` | arp_cache_poisoning | 28 |
| `network_under_attack` | `bgp_hijacking` | bgp_hijacking | 9 |
| `network_under_attack` | `dhcp_spoofed_dns` | Some hosts can not access webservices. | 3 |
| `network_under_attack` | `dhcp_spoofed_gateway` | dhcp_spoofed_gateway | 3 |
| `network_under_attack` | `dhcp_spoofed_subnet` | dhcp_spoofed_subnet | 3 |
| `network_under_attack` | `web_dos_attack` | Users reports high latency when accessing some web services. | 13 |
| `resource_contention` | `dns_lookup_latency` | Users experience high latency when accessing web services. | 6 |
| `resource_contention` | `incast_traffic_network_limitation` | incast_traffic_network_limitation | 13 |
| `resource_contention` | `link_bandwidth_throttling` | link_bandwidth_throttling | 29 |
| `resource_contention` | `link_high_packet_corruption` | link_high_packet_corruption | 29 |
| `resource_contention` | `load_balancer_overload` | load_balancer_overload | 3 |
| `resource_contention` | `receiver_resource_contention` | receiver_resource_contention | 13 |
| `resource_contention` | `sender_application_delay` | sender_application_delay | 13 |
| `resource_contention` | `sender_resource_contention` | sender_resource_contention | 13 |
| **Total** | - | - | **702** |

Based on the above issues, we disclose a large public dataset of AI agents’ behavior for network troubleshooting, with more than 900 reasoning traces. See the [![Zenodo Dataset](https://img.shields.io/badge/Zenodo-17971675-blue?logo=zenodo)](https://zenodo.org/records/17971675).

## MCP Servers and Tools

This framework provides MCP servers under `src/nika/service/mcp_server`. These include:

- **Host MCP server** (`common/host_server.py`, registered as `kathara_base_mcp_server`): host reachability and diagnostics, including
  - `get_reachability` to ping all pairs of hosts (subset when the lab is large).
  - `ping_pair` to ping between two specific hosts.
  - `iperf_test` to run an iperf test between two hosts.
  - `systemctl_ops` to manage system services (start, stop, restart, status).
  - `get_host_net_config` to retrieve the network configuration of a host.
  - `get_tc_statistics`, `netstat`, `ip_addr_statistics`, `ethtool`, `curl_web_test` for interface and service checks.
  - `cat_file`, `exec_shell`, `exec_shell_dual` to read files or run commands in containers.
- **BMv2 MCP server** (`kathara/bmv2_server.py`, registered as `kathara_bmv2_mcp_server`): P4/BMv2 switch interaction, including
  - `bmv2_get_log`, `bmv2_get_counter_arrays`, `bmv2_read_p4_program`, `bmv2_counter_read`.
  - `bmv2_show_tables`, `bmv2_table_dump`, `bmv2_get_register_arrays`, `bmv2_register_read`.
- **FRR MCP server** (`kathara/frr_server.py`, registered as `kathara_frr_mcp_server`): FRRouting routers, including
  - `frr_get_bgp_conf`, `frr_get_ospf_conf`, `frr_show_running_config`, `frr_show_ip_route`, `frr_exec`.
- **Telemetry MCP server** (`kathara/telemetry_server.py`, registered as `kathara_telemetry_mcp_server`): INT/InfluxDB telemetry, including
  - `influx_list_buckets`, `influx_get_measurements`, `influx_count_measurements`, `influx_query_measurement`.
- **Containerlab SR Linux MCP server** (`containerlab/srl_server.py`, registered as `containerlab_srl_mcp_server`): SR Linux routing diagnostics for Containerlab scenarios.
- **Task management MCP server** (`common/task_server.py`, registered as `task_mcp_server`): agent submissions, including
  - `list_avail_problems` to list injectable root-cause ids.
  - `submit` to write the agent's final detection/localization/RCA answer.

💡 More tools are coming soon...

You can also plug in your own MCP servers following the configuration instruction. Look for more MCP servers at [mcp.so](https://mcp.so/).



## Logging and Observability

Each session directory under `{result_dir}/{session_id}/` (default `{result_dir}` = `results/`) contains:

- **`events.jsonl`**: pipeline events (env deploy, fault inject, agent start/end, eval).
- **`messages.jsonl`**: agent conversation and tool traces.

Langfuse is optional and loaded only when `NIKA_LANGFUSE_ENABLED=true`. Keys: `.env.example`. Custom loggers: `src/agent/utils/loggers.py` and **[src/agent/README.md](src/agent/README.md)**.

<h1 id="📚cite">📚 Cite</h1>

```bibtex
@misc{nika,
      title={A Network Arena for Benchmarking AI Agents on Network Troubleshooting}, 
      author={Zhihao Wang and Alessandro Cornacchia and Alessio Sacco and Franco Galante and Marco Canini and Dingde Jiang},
      year={2025},
      eprint={2512.16381},
      archivePrefix={arXiv},
      primaryClass={cs.NI},
      url={https://arxiv.org/abs/2512.16381}, 
}
```

```bibtex
@inproceedings{llm4netlab,
author = {Wang, Zhihao and Cornacchia, Alessandro and Galante, Franco and Centofanti, Carlo and Sacco, Alessio and Jiang, Dingde},
title = {Towards a Playground to Democratize Experimentation and Benchmarking of AI Agents for Network Troubleshooting},
year = {2025},
isbn = {9798400720871},
publisher = {Association for Computing Machinery},
address = {New York, NY, USA},
url = {https://doi.org/10.1145/3748496.3748990},
doi = {10.1145/3748496.3748990},
booktitle = {Proceedings of the 1st Workshop on Next-Generation Network Observability},
pages = {1–3},
numpages = {3},
location = {Coimbra, Portugal},
series = {NGNO '25}
}
```

# Acknowledgement

This project is largely motivated by [AIOpsLab](https://github.com/microsoft/AIOpsLab). We sincerely thank the authors for their excellent work.

# Licence

Licensed under the MIT license.
