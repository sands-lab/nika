# NIKA CLI reference

Python package: `nika.cli` (directory `src/nika/cli/`).

Entry point: `nika` (see `[project.scripts]` in `pyproject.toml`). During development use `uv run nika …`.

Runtime paths (`runtime/`, `results/`, `benchmark/`) resolve from the repository root (derived from the installed `nika` package location). A `.env` file at the repo root is loaded automatically.

## Command tree

| Group | Purpose |
|--------|---------|
| `nika session` | List, inspect, and close active troubleshooting sessions |
| `nika env` | List / deploy Kathará or Containerlab scenarios and create a session |
| `nika failure` | List, describe, inject, and inspect faults for a running session |
| `nika exec` | Run a shell command inside a lab host container |
| `nika agent` | Run a troubleshooting agent on one selected session task |
| `nika eval` | Metrics, LLM judge, and offline summary CSV for closed sessions |
| `nika benchmark` | Full pipeline for benchmark YAML rows or a single `(scenario, problem)` case |
| `nika traffic` | Synthetic traffic (`od`, `web`) against the running lab |

Use `nika <group> --help` and `nika <group> <command> --help` for generated option text.

## Global conventions

### Sessions and `--session_id`

- **`nika env run`** prints `session_id=…` and writes `runtime/sessions/{session_id}.json`.
- Most commands that operate on a lab accept **`--session_id`** to target a specific session.
- When **`--session_id` is omitted** and exactly **one** session is running, that session is selected automatically. With zero or multiple running sessions, the CLI raises an error asking you to pass `--session_id` or reduce concurrency.
- **`nika session close`** undeploys the lab and clears runtime session state (confirmation prompt skippable with `-y` / `--yes`).

### Topology size (`-s` / `--size`)

Same semantics as `nika env run`:

- **Scalable** scenarios (see `TOPO_SIZE` on lab classes under `src/nika/net_env`) require **`-s s`**, **`-s m`**, or **`-s l`**.
- **Non-scalable** scenarios must **omit** `-s`.

This flag is reused on **`nika benchmark run`** and **`nika traffic run`** when a size is required and not already implied by the session.

### Results directory (`--result_dir`)

Session artifacts are written under **`{result_dir}/{session_id}/`**. Use this to isolate experiments (different datasets, models, agents, or benchmark runs) under separate folders.

| Source | Variable / flag | Default |
|--------|-----------------|---------|
| CLI | `--result_dir PATH` on `nika env run`, `nika benchmark run` | `results/` at repo root |
| `.env` | `NIKA_RESULT_DIR` | same as default |

CLI `--result_dir` overrides `NIKA_RESULT_DIR` when both are set. Relative paths resolve from the repository root (e.g. `results/list1` → `<repo>/results/list1/`).

```shell
nika env run simple_bgp --result_dir results/list1
# → results/list1/20260702-053412-abc123/

NIKA_RESULT_DIR=results/gpt4-bgp nika benchmark run --config benchmark/benchmark_selected.yaml
```

**Benchmark resume** (batch mode, `--resume` by default): before running, NIKA scans **only** the resolved `--result_dir` for existing session dirs. Rows whose `run.json` has `status == finished` and a matching `benchmark_fingerprint` are skipped; incomplete sessions are cleaned and re-run. Re-run the same command with the same `--config` and `--result_dir` to continue after a failure. Pass **`--no-resume`** to execute every YAML row regardless of existing artifacts.

### Agent options

Aligned with `nika agent run`:

- **`-a` / `--agent`**: `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `local_cli.codex_cli`, `local_cli.claude_cli`, `community.sade`, `sdk.claude_sdk`, or `sdk.codex_sdk`.
- **`-p` / `--provider`**: LLM provider for `byo.langgraph` only (`openai`, `ollama`, `deepseek`, `custom`).
- **`-m` / `--model`**: model id.
- **`-n` / `--max-steps`**: max steps per phase (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, `sdk.claude_sdk`).
- **`-e` / `--reasoning-effort`**: Codex `model_reasoning_effort` (`local_cli.codex_cli`, `sdk.codex_sdk`): `none`, `minimal`, `low`, `medium`, `high`, `xhigh`.

`nika eval judge` uses **`-p`** and **`-m`** for the judge only (no agent in that command).

### Benchmark judge options

`nika benchmark run` configures **both** agent and judge in one command. By default it runs **metrics only**; pass **`--judge`** to also run the LLM judge. Judge options use a **prefix** to avoid clashing with the agent:

- **`--judge`**: enable LLM-as-judge after metrics.
- **`--judge-provider`**
- **`--judge-model`**

Both judge options are required when **`--judge`** is set.

---

## `nika session`

- **`nika session ps [-a]`**: list sessions. Default: running only; **`-a` / `--all`** includes finished sessions. Columns: session id, env id, status, failure count, agent summary.
- **`nika session inspect [--session_id ID] [-c]`**: print the session document as JSON plus a table of `failure_injections`. Pass **`-c` / `--containers`** to also list running lab containers (docker-ps style). Auto-selects when only one session is running.
- **`nika session containers [--session_id ID]`**: list containers in the session lab (CONTAINER ID, NAME, IMAGE, STATUS, NAMES). Auto-selects when only one session is running.
- **`nika session close [--session_id ID] [-y]`**: undeploy the lab, mark failure records ended, and remove the runtime session file. When `--session_id` is omitted and only one session is running it is selected automatically; **`-y`** skips the confirmation prompt.
- **`nika session wipe [-y]`**: close every running session and wipe all leftover Kathara, Containerlab, and runtime working files.

---

## `nika env`

- **`nika env list`**: print registered scenario ids.
- **`nika env run NAME [-s s|m|l] [--no-redeploy] [--instance-tag TAG]`**: deploy one instance, create a session, and print `session_id=…`.
- **`nika env ps`**: list running lab instances (one row per deployed lab). Columns: env id, size, status, age, active session count, endpoint.

---

## `nika failure`

- **`nika failure list`**: injectable problem ids.
- **`nika failure describe PROBLEM`**: print the typed parameter schema (JSON Schema) and an example `nika failure inject … --set …` line.
- **`nika failure inject PROBLEM [PROBLEM …] [--session_id ID] [--set key=value …]`**: inject for a selected running session and write ground truth. Repeat **`--set`** to override injection parameters (see `describe` for valid keys).
- **`nika failure ps [--session_id ID]`**: list persisted failure injection records for one session.

---

## `nika exec`

Run a shell command inside a host container for the selected session-bound lab:

```shell
nika exec HOST COMMAND… [--session_id ID] [--timeout SECONDS]
```

- **`HOST`**: container / pc name in the lab (e.g. `pc1`).
- **`COMMAND`**: passed to the container shell (remaining args are joined with spaces).
- **`--timeout`**: default `10` seconds.

Example: `nika exec pc1 ping -c 3 10.0.0.2 --timeout 30`

---

## `nika agent`

- **`nika agent list`**: supported agent types (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `local_cli.codex_cli`, `local_cli.claude_cli`, `community.sade`, `sdk.claude_sdk`, `sdk.codex_sdk`), LLM providers, and Codex reasoning-effort levels.
- **`nika agent run`**: run the agent on one selected session.

  | Flag | Applies to | Meaning |
  |------|------------|---------|
  | `-a` / `--agent` | all | `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `local_cli.codex_cli`, `local_cli.claude_cli`, `community.sade`, `sdk.claude_sdk`, or `sdk.codex_sdk` |
  | `-p` / `--provider` | `byo.langgraph` | `openai`, `ollama`, `deepseek`, or `custom` |
  | `-m` / `--model` | all | model id |
  | `-n` / `--max-steps` | `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, `sdk.claude_sdk` | step cap per phase |
  | `-e` / `--reasoning-effort` | `local_cli.codex_cli`, `sdk.codex_sdk` | Codex reasoning effort level |
  | `--session_id` | all | target session |

  Examples:

  ```shell
  nika agent run -a byo.langgraph -p openai -m gpt-5-mini -n 20
  nika agent run -a local_cli.codex_cli -m gpt-5.4-mini -e medium
  ```

---

## `nika eval`

Eval commands operate on **closed** sessions only. Close the lab with **`nika session close`** before running eval; artifacts are read from and written to `results/{session_id}/`.

- **`nika eval metrics [--session_id ID] [--result_dir PATH]`**: rule-based metrics → `eval_metrics.json` (records eval completion in `events.jsonl`). With `--result_dir` and no `--session_id`, runs on every closed session under that directory.
- **`nika eval judge -p PROVIDER -m MODEL [--session_id ID] [--result_dir PATH]`**: LLM judge → `llm_judge.json`. With `--result_dir` and no `--session_id`, judges every closed session under that directory.
- **`nika eval summary [filters] [-o PATH] [--result_dir PATH]`**: scan finished sessions and write one CSV.
- **`nika eval clean [-y] [--force]`**: delete historical artifacts under `results/`, runtime session JSON files, and the SQLite index at `runtime/sessions.db`. Refuses when running sessions exist unless **`--force`** is passed.

### `nika eval summary` filters

All filters are optional and repeatable. Omit filters to include every finished session that has the required artifacts.

| Option | Meaning |
|--------|---------|
| `-o` / `--output` | Output CSV path (default: `{result_dir}/0_summary/evaluation_summary.csv`) |
| `--result_dir` | Results parent directory to scan (default: `results/` or `NIKA_RESULT_DIR`) |
| `-p` / `--problem` | Root-cause / problem id (e.g. `link_down`) |
| `-e` / `--env` | Scenario / net env (e.g. `simple_bgp`) |
| `-c` / `--category` | Root-cause category (e.g. `link_failure`) |
| `--session_id` | Specific session id |
| `-a` / `--agent` | Agent type |
| `--model` | Agent model id |

Each finished session directory should contain at least `run.json`, `ground_truth.json`, and `eval_metrics.json`. `llm_judge.json` is optional and merged when present.

---

## `nika benchmark`

Implements the end-to-end benchmark pipeline: start env → inject → agent → close session → eval (metrics, optional judge). Run `nika eval summary` afterward to aggregate CSV rows across finished sessions.

### Batch mode (default)

Omit the `SCENARIO` positional argument. Rows are read from a YAML file.

```shell
nika benchmark run
nika benchmark run --config benchmark/benchmark_selected.yaml
nika benchmark run --batch-size 4
nika benchmark run --result_dir results/list1
nika benchmark run --result_dir results/list1 --batch-size 4   # resume skips completed rows in that dir only
```

**Default config path**: `benchmark/benchmark_selected.yaml` under the repository root.

**`--result_dir`**: parent directory for session outputs (see [Results directory](#results-directory---result_dir)). Resume and skip logic inspect **only** this directory—not other folders under `results/` and not the SQLite index.

**`--resume` / `--no-resume`** (batch mode): when `--resume` (default), scan `--result_dir` first, skip finished cases, clean incomplete ones, then run the rest. Works with any `--batch-size`.

**`--batch-size`**: number of YAML rows to run simultaneously per batch (default `1`). Rows are chunked into groups of this size; each group runs fully in parallel (one subprocess per row) and the next group starts only after all rows in the current group have finished. Applies to batch mode only.

**`--case-timeout SECONDS`** (`NIKA_CASE_TIMEOUT`, batch mode): hard per-case time limit. When it expires, the case's entire process group is killed (agent subprocesses and docker clients included) and the case counts as failed. When set, each row runs in its own subprocess even at `--batch-size 1`, so a stuck case can be killed cleanly.

**`--continue-on-error`** (`NIKA_CONTINUE_ON_ERROR`, batch mode): keep going after a failed case instead of aborting the run; failed cases are summarized at the end. Re-running the same command with `--resume` retries only the failed cases.

**`--retry-passes N`** (`NIKA_RETRY_PASSES`, batch mode): after the first pass, automatically re-scan and retry failed cases up to `N` extra passes (implies `--continue-on-error`). Retries stop early when a pass completes no new case. Example for a long unattended run:

```bash
nika benchmark run --config benchmark/benchmark_selected.yaml --batch-size 4 \
    --case-timeout 2400 --retry-passes 2
```

**YAML case fields**:

| Field | Meaning |
|-------|---------|
| `problem` | Problem id (same as `nika failure inject`) |
| `scenario` | Scenario id (same as `nika env run`) |
| `topo_size` | Size `s`, `m`, or `l`; **null/empty** for scenarios without sizes |
| `inject` | Map of `--set key=value` pairs passed to `nika failure inject` |

Agent and judge options use the same flags as below (including `-a local_cli.codex_cli` and `-e` for Codex runs; `-n` applies to `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, and `community.sade`).

### Single-case mode

Pass **`SCENARIO`** as the first positional argument (like `nika env run NAME`), plus **`--problem`**:

```shell
nika benchmark run dc_clos_bgp --problem bgp_asn_misconfig -s s \
  -a byo.langgraph -p openai -m gpt-5-mini -n 20 \
  --judge --judge-provider openai --judge-model gpt-5-mini
```

- **`-s` / `--size`**: required only when `SCENARIO` is scalable.
- **`--judge`**: optional; without it, only metrics run after the agent finishes.
- Each benchmark case gets its own lab; the lab is torn down when the session closes (before evaluation).

---

## `nika traffic`

Requires a deployed lab. By default the **current session** supplies the deployed lab name and size; override with **`--lab`** (and **`-s`** when the scenario needs a size).

- **`nika traffic list`**: supported **`TYPE`** values for `run`.
- **`nika traffic run TYPE …`**: start traffic; options depend on **`TYPE`**.

### Foreground vs background (`--background`)

| TYPE | `--no-background` (default) | `--background` |
|------|------------------------------|------------------|
| `od` | Run iperf3 clients synchronously; print JSON summaries to stdout | Start iperf3 in the background inside the lab; print a short JSON list of flow labels |
| `web` | Block until interrupted or finished (`--no-loop`) | **Not supported** (error): web load always blocks this CLI |

### `nika traffic run od`

OD-matrix iperf3 using `ODFLowGenerator`.

**Exactly one** traffic pattern:

1. **`--od-json PATH`**: JSON object `{ "src_host": { "dst_host": <rate>, ... }, ... }` (rates match `--unit`).
2. **`--mesh-mbps N`**: every ordered pair of distinct hosts in the scenario at `N` Mbit/s (with `--unit M`).
3. **`--all-to-host H --mbps N`**: every host except `H` sends to `H` at `N` Mbit/s (same pattern as bandwidth-throttling examples).

Shared iperf tuning:

- **`--interval`**: iperf `-t` duration (seconds).
- **`--unit`**: `K` or `M` (bitrate suffix for matrix values).
- **`--udp` / `--no-udp`**
- **`--server-args`**, **`--client-args`**: extra iperf3 arguments.

### `nika traffic run web`

Uses `WebBrowsingTrafficGenerator` (ApacheBench against `web_urls`). Only scenarios that define web servers and URLs are valid.

Options:

- **`--request-delay-min`**, **`--request-delay-max`**
- **`--pages-min`**, **`--pages-max`**
- **`--no-loop`**: one browsing session per client host then exit

---

## Helpful paths

- Runtime sessions: `runtime/sessions/*.json` (cleared when a session is finished)
- Eval summary CSV default: `results/0_summary/evaluation_summary.csv`
- Benchmark data: `benchmark/*.yaml` under the repo root
