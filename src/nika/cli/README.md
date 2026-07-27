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
| `nika leaderboard` | Pack, validate, and submit leaderboard entries (GitHub PR) from official release runs |
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

- **Ad-hoc / single-case / env**: session artifacts under **`{result_dir}/{session_id}/`** for bare single-case CLI; batch `--config` uses the same **`{result_dir}/trials/{case_key}__t01/`** trials/ layout as release runs (`n_trials=1`).
- **Release run**: `--result_dir` **is** one run — `{result_dir}/run.json` plus `{result_dir}/trials/{case_key}__tNN/`.

Use separate directories to isolate experiments (different datasets, models, agents, or release runs).

| Source | Variable / flag | Default |
|--------|-----------------|---------|
| CLI | `--result_dir PATH` on `nika env run`, `nika benchmark run` | `results/` at repo root |
| `.env` | `NIKA_RESULT_DIR` | same as default |

CLI `--result_dir` overrides `NIKA_RESULT_DIR` when both are set. Relative paths resolve from the repository root (e.g. `results/list1` → `<repo>/results/list1/`).

```shell
nika env run simple_bgp --result_dir results/list1
# → results/list1/20260702-053412-abc123/

nika benchmark run --release 0.1.0 --result_dir results/my-release-run
# → results/my-release-run/run.json
# → results/my-release-run/trials/<case_key>__t01/ …

NIKA_RESULT_DIR=results/gpt4-bgp nika benchmark run --config benchmark/benchmark_selected.yaml
```

**Benchmark resume** (batch mode, `--resume` by default): before running, NIKA scans **only** the resolved `--result_dir` trials. Completed trials (`outcome` in `{success, agent_failed}`) are skipped; incomplete dirs are cleaned and re-run in place. Pass **`--no-resume`** to execute every trial regardless of existing artifacts.

### Agent options

Aligned with `nika agent run`:

- **`-a` / `--agent`**: `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `local_cli.codex_cli`, `local_cli.claude_cli`, `community.sade`, `sdk.claude_sdk`, or `sdk.codex_sdk`.
- **`-p` / `--provider`**: LLM provider for `byo.langgraph` only (`openai`, `ollama`, `deepseek`, `custom`).
- **`-m` / `--model`**: model id.
- **`-n` / `--max-steps`**: max steps per phase (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, `sdk.claude_sdk`).
- **`-e` / `--reasoning-effort`**: Codex `model_reasoning_effort` (`local_cli.codex_cli`, `sdk.codex_sdk`): `none`, `minimal`, `low`, `medium`, `high`, `xhigh`.

`nika eval judge` uses **`-p`** and **`-m`** for the judge only (no agent in that command).

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

Eval commands operate on **closed** sessions only. After a benchmark run (or a manual `nika session close`), use eval for post-hoc scoring:

- **`nika eval metrics [--session_id ID] [--result_dir PATH]`**: rule-based metrics → `eval_metrics.json` (records eval completion in `events.jsonl`). With `--result_dir` and no `--session_id`, runs on every closed session under that directory. Benchmark already writes metrics when each case closes; re-run this to recompute.
- **`nika eval judge -p PROVIDER -m MODEL [--session_id ID] [--result_dir PATH]`**: LLM judge → `llm_judge.json`. With `--result_dir` and no `--session_id`, judges every closed session under that directory.
- **`nika eval summary [filters] [-o PATH] [--result_dir PATH]`**: scan finished sessions and write one CSV.
- **`nika eval clean [-y] [--force]`**: delete historical artifacts under `results/`, runtime session JSON files, and the SQLite index at `runtime/sessions.db`. Refuses when running sessions exist unless **`--force`** is passed.

Typical post-benchmark flow:

```shell
nika benchmark run --release 0.1.0 --result_dir results/my_run
nika eval judge -p openai -m gpt-5-mini --result_dir results/my_run   # optional
nika eval summary --result_dir results/my_run
```

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

## `nika leaderboard`

Pack, validate, and open a GitHub PR for a leaderboard submission from an official release run. See [`docs/leaderboard-submission.md`](../../docs/leaderboard-submission.md).

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard pack --result_dir results/my-run \
  --submission results/my-run/submission

nika leaderboard validate results/my-run/YYYYMMDD_slug \
  --source-result-dir results/my-run

nika leaderboard submit results/my-run/YYYYMMDD_slug
```

Pack flags: `--result_dir`, required `--submission`, optional `--out`. Pack writes `{result_dir}/{YYYYMMDD}_{slug}/` by default. Submit requires authenticated [`gh`](https://cli.github.com/) and opens a PR on `sands-lab/nika-leaderboard` (override with `--repo`).

---

## `nika benchmark`

Implements the experiment pipeline: start env → inject → agent → close session → rule-based metrics. Use **`nika eval judge`** / **`nika eval summary`** afterward for LLM judge and CSV aggregation.

### Batch mode

Omit the `SCENARIO` positional argument and pass either **`--release`** (frozen suite) or **`--config`** (ad-hoc YAML). There is no bare default suite.

```shell
nika benchmark run --release 0.1.0              # frozen release
nika benchmark run --release 0.1.0 --result_dir results/my-run
nika benchmark releases                         # list + verify each release
nika benchmark run --config benchmark/benchmark_selected.yaml
nika benchmark run --release 0.1.0 --batch-size 4
nika benchmark run --release 0.1.0 --result_dir results/list1
nika benchmark run --release 0.1.0 --result_dir results/list1 --batch-size 4
```

**Release preflight**: `nika benchmark run --release …` and `nika benchmark releases` check case count / `cases_sha256` / `benchmark_digest`, Dev∩Test fingerprint isolation, scenario/problem registration, source-file pins, MCP allowlist, and required Docker images (images must already exist; release mode does not auto-build).

Release runs expand each case to `defaults.n_trials` trials (3 for `0.1.0`) under `{result_dir}/trials/{case_key}__tNN/`. Ad-hoc `--config` uses the same layout with `n_trials=1`. Resume skips completed trials (including `agent_failed`); incomplete trials are re-run without creating extra trial indices.

**Artifacts**: release runs write `run.json` (and legacy `benchmark_job.json`) plus `RELEASE.lock.json` under `--result_dir`, and stamp each trial `run.json` with `benchmark_id` / `benchmark_version` / `benchmark_digest` / `benchmark_split` / `nika_git_commit` / `scoring_id` / `trial_id` / `outcome`. Live run progress (completed/pending trials) is written to `runtime/benchmark_runs/{run_id}.json`.

**`--result_dir`**: for batch `--config` / `--release` this directory **is** the run root (see [Results directory](#results-directory---result_dir)). Resume and skip logic inspect **only** this directory—not other folders under `results/` and not the SQLite index.

**`--resume` / `--no-resume`** (batch mode): when `--resume` (default), scan `--result_dir` first, skip finished trials, clean incomplete ones, then run the rest. Works with any `--batch-size`.

**`--batch-size`**: number of trials to run simultaneously per batch (default `1`). Trials are chunked into groups of this size; each parallel group runs via spawn processes (and timeouts also use spawn). Applies to batch mode only.

**`--case-timeout SECONDS`** (`NIKA_CASE_TIMEOUT`, batch mode): hard per-trial time limit. When omitted, the release default is used (**2400** for `0.1.0`); ad-hoc `--config` defaults to `0` (disabled). When set, each trial runs in an isolated spawn process so a stuck case can be killed cleanly.

**`--continue-on-error`** (`NIKA_CONTINUE_ON_ERROR`, batch mode): keep going after a failed trial instead of aborting the run; failures are summarized at the end. Re-running the same command with `--resume` retries only incomplete trials (counted `agent_failed` trials are kept).

**`--retry-passes N`** (`NIKA_RETRY_PASSES`, batch mode): after the first pass, automatically re-scan and retry incomplete trials up to `N` extra passes (implies `--continue-on-error`). Never overwrites `agent_failed` trials. Retries stop early when a pass completes no new trial. Example for a long unattended run:

```bash
nika benchmark run --release 0.1.0 --batch-size 4 --retry-passes 2 --result_dir results/my-run
```

**YAML case fields**:

| Field | Meaning |
|-------|---------|
| `problem` | Problem id (same as `nika failure inject`) |
| `scenario` | Scenario id (same as `nika env run`) |
| `topo_size` | Size `s`, `m`, or `l`; **null/empty** for scenarios without sizes |
| `inject` | Map of `--set key=value` pairs passed to `nika failure inject` |

Agent options use the same flags as below (including `-a local_cli.codex_cli` and `-e` for Codex runs; `-n` applies to `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, and `community.sade`).

### Single-case mode

Pass **`SCENARIO`** as the first positional argument (like `nika env run NAME`), plus **`--problem`**:

```shell
nika benchmark run dc_clos_bgp --problem bgp_asn_misconfig -s s \
  -a byo.langgraph -p openai -m gpt-5-mini -n 20
nika eval judge -p openai -m gpt-5-mini --result_dir results/
nika eval summary --result_dir results/
```

- **`-s` / `--size`**: required only when `SCENARIO` is scalable.
- Each benchmark case gets its own lab; the lab is torn down when the session closes (before metrics).
- LLM judge and CSV summary are separate `nika eval` steps, not part of `benchmark run`.

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
