# NIKA CLI reference

This reference is for operators and script authors who need the complete NIKA command surface. The Python package lives in `src/nika/cli/`.

Implementation: [`main.py`](../../src/nika/cli/main.py) registers the command groups; [`commands/`](../../src/nika/cli/commands/) contains option parsing and delegates reusable behavior to [`workflows/`](../../src/nika/workflows/).

Entry point: `nika` (see `[project.scripts]` in `pyproject.toml`). During development use `uv run nika …`.

Runtime paths (`runtime/`, `results/`, `benchmark/`) resolve from the repository root (derived from the installed `nika` package location). Credentials load from the repository-root `.env`; operational settings load from `config/nika.yaml`. See the [run configuration reference](configuration.md).

## Command tree

| Group | Purpose |
|--------|---------|
| `nika session` | List, inspect, and close active troubleshooting sessions |
| `nika env` | List / deploy Kathará or Containerlab scenarios and create a session |
| `nika failure` | List, describe, inject, and inspect faults for a running session |
| `nika exec` | Run a shell command inside a lab host container |
| `nika config` | Show effective run config or migrate legacy `.env` ops into YAML |
| `nika agent` | Run an end-to-end task, or run an agent on a selected session |
| `nika benchmark` | Full pipeline for benchmark YAML rows or a single `(scenario, problem)` case |
| `nika eval` | Metrics, LLM judge, and offline summary CSV for closed sessions |
| `nika leaderboard` | Pack, validate, and submit scores (GitHub PR) + trajectories (HF PR) from official release runs |
| `nika inspect` | Local web UI for session trajectories, merged timelines, and scores |
| `nika remote` | Optional lab-host control plane (`serve` / `health`); see [remote lab execution](remote.md) |
| `nika traffic` | Synthetic traffic (`od`, `web`, `sndlib`) against the running lab |

Use `nika <group> --help` and `nika <group> <command> --help` for generated option text.

## Global conventions

### Sessions and `--session_id`

- **`nika env run`** prints `session_id=…` and writes `runtime/sessions/{session_id}.json`.
- Most commands that operate on a lab accept **`--session_id`** to target a specific session.
- If you omit **`--session_id`** and one session is running, NIKA selects that session. With zero or multiple running sessions, pass `--session_id` or close extra sessions.
- **`nika session close`** undeploys the lab and clears runtime session state (confirmation prompt skippable with `-y` / `--yes`).

### Topology size (`-s` / `--size`)

Same semantics as `nika env run`:

- **Scalable** scenarios (see `TOPO_SIZE` on lab classes under `src/nika/net_env`) require **`-s s`**, **`-s m`**, or **`-s l`**.
- **Non-scalable** scenarios must **omit** `-s`.

This flag is reused on **`nika benchmark run`** and **`nika traffic run`** when a size is required and not already implied by the session.

### Results directory (`--result_dir`)

`--result_dir` chooses the parent folder for session and benchmark artifacts. Default is `results/` at the repository root. You can also set `nika.result_dir` in `config/nika.yaml`. The CLI flag wins over YAML. Relative paths resolve from the repository root (`results/list1` → `<repo>/results/list1/`).

Use a fresh directory for each experiment (agent, model, release split, or ad-hoc YAML). Resume and skip look only inside the directory you pass.

| Command | Layout under `--result_dir` |
|---------|-----------------------------|
| `nika env run` | `{session_id}/` (timestamp-style id) |
| `nika benchmark run SCENARIO --problem …` | `{session_id}/` |
| `nika benchmark run --config …` | `run.json` plus `trials/{task_id}__t01/` (`n_trials=1`) |
| `nika benchmark run --release …` | `run.json` plus `trials/{task_id}__tNN/` (`n_trials` from the release) |

`task_id` is the public case id (see [Task ids](#task-ids)). A trial directory name is `{task_id}__t01`, `{task_id}__t02`, and so on.

```shell
nika env run dc_clos -s s --result_dir results/list1
# → results/list1/20260702-053412-abc123/

nika benchmark run --config benchmark/working/cases.yaml --result_dir results/my-run
# → results/my-run/run.json
# → results/my-run/trials/<task_id>__t01/ …
```

Batch resume only looks at this directory. See [`--resume`](#batch-run-options).

### Agent options

Aligned with `nika agent run`:

- **`-a` / `--agent`**: `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `cli.codex`, `cli.claude`, `community.sade`, `sdk.claude_sdk`, or `sdk.codex_sdk`.
- **`-p` / `--provider`**: LLM provider for all agents (`openai`, `anthropic`, `deepseek`, `custom`; capabilities differ by agent).
- **`-m` / `--model`**: model id.
- **`-n` / `--max-steps`**: max steps per phase (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, `sdk.claude_sdk`).
- **`-e` / `--reasoning-effort`**: Reasoning effort for BYO agents (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`), `cli.codex`, and `sdk.codex_sdk`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. `byo.mcp_agent` accepts `none` / `low` / `medium` / `high` only.
- **`--base-url`**: inference endpoint (`agent.custom.base_url`). Required for `provider=custom`; also overrides the OpenAI or Anthropic base URL.

`nika eval judge` uses **`-p`** and **`-m`** for the judge only (no agent in that command).

---

## `nika session`

- **`nika session ps [-a]`**: list sessions. Default: running only; **`-a` / `--all`** includes finished sessions. Columns: session id, env id, status, failure count, agent summary.
- **`nika session inspect [--session_id ID] [-c]`**: print the session document as JSON plus a table of `failure_injections`. Pass **`-c` / `--containers`** to also list running lab containers (docker-ps style). Auto-selects when only one session is running.
- **`nika session containers [--session_id ID]`**: list containers in the session lab (CONTAINER ID, NAME, IMAGE, STATUS, NAMES). Auto-selects when only one session is running.
- **`nika session close [--session_id ID] [-y]`**: undeploy the lab, mark failure records ended, and remove the runtime session file. If you omit `--session_id` with one running session, NIKA selects it. **`-y`** skips the confirmation prompt.
- **`nika session wipe [-y]`**: close every running session and wipe all leftover Kathara, Containerlab, and runtime working files.

---
---

## `nika inspect`

Open a local web UI over a results root: folder tree, filters, merged agent/NIKA timeline, scores, and raw artifacts.

How-to (front-end build, bind host, Vite): [Browse sessions with `nika inspect`](session-inspect.md).

```shell
nika inspect [--result-dir PATH] [--host 127.0.0.1] [--port 7580] [--no-open]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--result-dir` | `nika.result_dir` / `results/` | Results root to scan (session dirs with `run.json`) |
| `--host` | `127.0.0.1` | `127.0.0.1`, `localhost`, `::1`, `0.0.0.0`, or `::` (not a specific NIC IP) |
| `--port` | `7580` | If busy, NIKA picks a free port and prints it |
| `--no-open` | off | Skip opening a browser (always skipped under SSH or WSL) |

---

## `nika env`

- **`nika env list`**: print registered scenario ids.
- **`nika env run NAME [-s s|m|l] [--static-validation|--no-static-validation] [--no-redeploy] [--instance-tag TAG]`**: deploy one instance, run configured runtime verification, create a session, and print `session_id=…`. Batfish follows `nika.static_validation.enabled` (CLI flags override it for one run). Runtime depth follows `nika.runtime_validation.depth` (default `light`).
- **`nika env ps`**: list running lab instances (one row per deployed lab). Columns: env id, size, status, age, active session count, endpoint.

---
---

## `nika failure`

- **`nika failure list`**: injectable problem ids.
- **`nika failure describe PROBLEM`**: print the typed parameter schema (JSON Schema) and an example `nika failure inject … --set …` line.
- **`nika failure inject PROBLEM [PROBLEM …] [--session_id ID] [--set key=value …]`**: inject for a selected running session and write ground truth. Repeat **`--set`** to override injection parameters (see `describe` for valid keys).
- **`nika failure ps [--session_id ID]`**: list persisted failure injection records for one session.

---
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
---

## `nika config`

- **`nika config show [--run-config PATH]`**: validate and print the effective non-secret run configuration. `--run-config` also accepts `NIKA_RUN_CONFIG`; the default path is `config/nika.yaml`.
- **`nika config set KEY=VALUE... [--run-config PATH]`**: write important keys into the YAML file (sparse update). Allowed keys: `agent.type`, `agent.provider`, `agent.model`, `agent.max_steps`, `agent.reasoning_effort`, `agent.custom.base_url`, `nika.result_dir`, `nika.enable_skills`, `nika.judge.provider`, `nika.judge.model`. Lab/MCP/k8s and other knobs stay YAML-only.
- **`nika config migrate [--env-file PATH] [-o PATH] [--write-env] [-y]`**: convert legacy operational `.env` keys into versioned YAML. It prints the proposed YAML before writing; confirm with `y` (`[y/N]`, default no). If `.env` has no ops keys, it tells you to prefer `cp config/nika.example.yaml config/nika.yaml`. With `--write-env`, it backs up `.env` and rewrites it to credential-only entries after confirmation; `-y` skips prompts.

The tracked template is `config/nika.example.yaml` (preferred for new setups). Precedence is CLI flags → YAML → built-in defaults. Use `--base-url` / `-m` / `-p` on `nika agent run` and `nika benchmark run` for one-shot overrides; use `nika config set` to persist. Provider API keys stay in the repo-root `.env`. Leftover operational keys in `.env` are ignored at runtime (NIKA prints a one-shot warning); migrate them instead of relying on env.

Lab deployment and verification timings live under `nika.lab`. MCP client and gateway settings live under `nika.mcp`. The `byo.langgraph` LLM client reads timeout and retry settings from `agent.llm`. See the [run configuration reference](configuration.md) for defaults and constraints.

---
---

## `nika agent`

- **`nika agent list`**: supported agent types (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `cli.codex`, `cli.claude`, `community.sade`, `sdk.claude_sdk`, `sdk.codex_sdk`), LLM providers, and reasoning-effort levels.
- **`nika agent run`**: two modes:

  1. **Task mode (recommended):** `--problem LABEL` runs the complete task lifecycle: deploy the lab, inject the fault (using defaults from the benchmark resolver), run the agent, close the session, and write metrics.
  2. **Session mode:** omit `--problem` and run against an already injected session (`nika env run` → `nika failure inject` → `nika agent run`).

  ### Task labels

  | Scenario type | Label | Example |
  |---------------|--------|---------|
  | Non-scalable | `{scenario}_{problem}` | `simple_bgp_link_down` |
  | Scalable | `{scenario}_{size}_{problem}` with size `s`, `m`, or `l` | `dc_clos_s_link_down` |

  Discover pieces with `nika env list` and `nika failure list`.

  | Flag | Mode | Meaning |
  |------|------|---------|
  | `-a` / `--agent` | both | `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `cli.codex`, `cli.claude`, `community.sade`, `sdk.claude_sdk`, or `sdk.codex_sdk` |
  | `-p` / `--provider` | both | Shared: `openai`, `anthropic`, `deepseek`, or `custom` |
  | `-m` / `--model` | both | model id |
  | `-n` / `--max-steps` | both | step cap per phase (`byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, `sdk.claude_sdk`) |
  | `-e` / `--reasoning-effort` | both | Reasoning effort (BYO agents, `cli.codex`, `sdk.codex_sdk`) |
  | `--base-url` | both | Inference endpoint (`agent.custom.base_url`) |
  | `--problem` | task | task label (see above) |
  | `--set key=value` | task | override inject parameters (repeatable) |
  | `--result_dir` | task | results parent directory |
  | `--session_id` | session | target session (mutually exclusive with `--problem`) |

  Examples:

  ```shell
  # Task mode: env → inject → agent → close + metrics
  nika agent run -a byo.langgraph -p openai -m gpt-5-mini \
    --problem dc_clos_s_link_down
  nika agent run -a byo.langgraph -p openai -m gpt-5-mini -n 20 \
    --problem dc_clos_s_link_down --set host_name=pc_0_0 --set intf_name=eth0

  # Session mode (manual lab control)
  nika env run dc_clos -s s
  nika failure inject link_down --set host_name=pc_0_0 --set intf_name=eth0
  nika agent run -a cli.codex -m gpt-5-mini -e medium
  nika session close -y
  ```

  Manual lab/session commands are documented under **`nika env`**, **`nika failure`**, and **`nika session`** below.

---
---

## `nika benchmark`

Implements the experiment pipeline: start env → inject → agent → close session → rule-based metrics. Use **`nika eval judge`** / **`nika eval summary`** afterward for LLM judge and CSV aggregation.

### Batch mode

Omit the `SCENARIO` positional argument to run a case matrix. Pass **`--release`** for a frozen suite or **`--config`** for a YAML file or candidate pool.

```shell
# Frozen release (0.1.0 is deprecated; use 0.2.0)
nika benchmark releases
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run

# Ad-hoc YAML or full candidate pool
nika benchmark run --config benchmark/working/cases.yaml --result_dir results/my-run
nika benchmark run                              # defaults to benchmark/working/pool
nika benchmark run --config benchmark/working/pool --batch-size 4
```

**Release preflight**: `nika benchmark run --release …` and `nika benchmark releases` check case counts, scenario/problem registration, MCP allowlist, and required Docker images. Missing images are built or pulled through the ordinary deployment path. Deprecated releases are reported and skipped.

### Task ids

Each benchmark case has a public `task_id`. NIKA derives it from scenario, problem, deploy options, and inject params. The same string is the filesystem `case_key`. Trial directories append `__tNN` (`{task_id}__t01`).

Use these commands when you need to look up a case or re-run one failed trial without rebuilding `--set` flags by hand:

```shell
nika benchmark list --release 0.2.0 --split test
nika benchmark describe dc_clos__link_detach__m__host_name-client_0__intf_name-eth0 \
  --release 0.2.0 --split test
nika benchmark run --release 0.2.0 --split test \
  --task-id dc_clos__link_detach__m__host_name-client_0__intf_name-eth0__t01 \
  --result_dir results/one-case
```

| Command | What it does |
|---------|----------------|
| `nika benchmark list` | Prints `SCENARIO`, `PROBLEM`, `DEPLOY`, and `TASK_ID` for a `--release` split or `--config` YAML. Add `--json` for scripts. Source defaults match `nika benchmark run` (configured release, otherwise the candidate pool). |
| `nika benchmark describe TASK_ID` | Prints scenario, problem, deploy fields, inject, and `root_causes`. Accepts a case id or a trial dirname; strips `__tNN`. |
| `nika benchmark run --task-id ID` | Batch mode only. Repeatable. A case id runs every trial for that case; `{task_id}__t02` runs that trial slot only. Unknown ids fail before the run starts. |

### Run layout and artifacts

Release runs expand each case to `defaults.n_trials` trials under `{result_dir}/trials/{task_id}__tNN/`. Ad-hoc `--config` uses the same layout with `n_trials=1`.

For batch `--config` or `--release`, `--result_dir` is the run root (see [Results directory](#results-directory---result_dir)). Resume and skip inspect only that directory.

Release runs write `run.json` (and legacy `benchmark_job.json`) plus `RELEASE.lock.json` under `--result_dir`. Each trial `run.json` is stamped with `benchmark_id` / `benchmark_version` / `benchmark_split` / `nika_git_commit` / `scoring_id` / `trial_id` / `outcome`. Live progress is under `runtime/benchmark_runs/{run_id}.json`.

### Console output

`nika benchmark run` first prints a compact **Plan** with **Done** / **Pending** label lists (no full paths) and asks for confirmation. The plan shows run counts plus **cases × trials/case**; progress is over flattened runs.

It then opens a Rich Live dashboard on the **alternate screen**: a fixed-height job panel (progress, scores, `trials/s` / `req/s`, token min/avg/max) and one fixed-height session box per running trial, whose trajectory scrolls inside the box. Width resize clears the surface; height-only resize does not.

Python library warnings are buffered and printed once afterward in a yellow **Warnings** panel. After the run you get the final dashboard frame on the main screen, Rich summary tables, an `nika inspect --result-dir …` hint, and Warnings if any.

| Flag | Effect |
|------|--------|
| `-y` / `--yes` | Skip the confirmation prompt (also skipped when stdin is not a TTY) |
| `-v` / `--verbose` | Full plan lists plus per-trial skip/clean/running lines |

### Batch run options

**`--resume` / `--no-resume`** (default: resume):

| `--resume` | `--no-resume` |
|------------|---------------|
| Skip trials with `outcome` in `{success, agent_failed}` | Delete every trial slot under the run |
| Rebuild metrics if a finished dir still lacks `outcome` | Re-run all trials |
| Delete incomplete / `running` dirs, then run the rest | |

Scoped to the current `--result_dir` only.

**`--batch-size`**: parallel trials per batch (default `1`). Parallel groups and timeouts use spawn processes. Batch mode only.

**`--case-timeout SECONDS`** (`benchmark.case_timeout_sec`, default **2400**; `0` disables): kill the trial worker when the budget expires. If `ground_truth.json` exists, finalize as `agent_failed` so `--resume` keeps the slot.

**Inner no-response timeout**: `nika.mcp.read_timeout_sec` in `config/nika.yaml` (default **120**). Hung MCP calls fail on that timer, not the full case budget. Lab `exec` stays ~10s. `max_steps` caps LLM turns, not wall time.

**`--continue-on-error` / `--abort-on-error`**: after a failed trial, either keep going (summarize at the end) or stop. Official `--release` defaults to continue; ad-hoc `--config` uses `benchmark.continue_on_error`. With `--resume`, only incomplete trials retry (`agent_failed` stays finished).

**`--retry-passes N`** (`benchmark.retry_passes`): after the first pass, retry incomplete trials up to `N` more times (implies `--continue-on-error`). Stops early if a pass finishes no new trial. `agent_failed` trials stay finished.

```bash
nika benchmark run --config benchmark/working/cases.yaml --batch-size 4 --retry-passes 2 --result_dir results/my-run
```

### Freeze a release

**`nika benchmark freeze --version VERSION --source DIRECTORY`** copies validated `dev.yaml` and `test.yaml` into `benchmark/releases/VERSION/`. It refuses to overwrite an existing destination.

### Migrate root causes

**`nika benchmark migrate`**: read a YAML case matrix with a top-level `cases` field, derive `root_causes` from injection parameters and topology, then write a report. The command writes unresolved rows and exits with status 1 unless `--allow-unresolved` is set. Do not pass a release `RELEASE.yaml` manifest. Working-matrix and release generation already materialize these labels. See [root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).

### YAML case fields

| Field | Meaning |
|-------|---------|
| `problem` | Problem id (same as `nika failure inject`) |
| `scenario` | Scenario id (same as `nika env run`) |
| `topo_size` | Size `s`, `m`, or `l`; **null/empty** for scenarios without sizes |
| `inject` | Map of `--set key=value` pairs passed to `nika failure inject` |
| `root_causes` | Materialized diagnoses (`resource` + `fault_type`); `resource_id` is derived on submit and scoring; see [root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md) |

Benchmark exposes `-a`, `-p`, `-m`, and `-n`; `-n` affects `byo.langgraph`, `byo.mcp_agent`, `byo.autogen`, `community.sade`, and `sdk.claude_sdk`. It does not expose `-e`; configure reasoning through `agent.reasoning_effort` in `config/nika.yaml` for benchmark runs.

### Single-case mode

Pass **`SCENARIO`** as the first positional argument (like `nika env run NAME`), plus **`--problem`**:

```shell
nika benchmark run dc_clos --problem bgp_asn_misconfig -s s \
  -a byo.langgraph -p openai -m gpt-5-mini -n 20
nika eval judge -p openai -m gpt-5-mini --result_dir results/
nika eval summary --result_dir results/
```

- **`-s` / `--size`**: required only when `SCENARIO` is scalable.
- Each benchmark case gets its own lab; the lab is torn down when the session closes (before metrics).
- LLM judge and CSV summary are separate `nika eval` steps, not part of `benchmark run`.

---
---

## `nika eval`

Eval commands operate on **closed** sessions only. After a benchmark run (or a manual `nika session close`), use eval for post-hoc scoring:

- **`nika eval metrics [--session_id ID] [--result_dir PATH]`**: write rule-based metrics to `eval_metrics.json` and record completion in `events.jsonl`. Scoring compares `(resource_id, fault_type)` pairs. With `--result_dir` and no `--session_id`, the command processes every closed session under that directory. Benchmark writes metrics when each case closes.
- **`nika eval judge -p PROVIDER -m MODEL [--session_id ID] [--result_dir PATH]`**: LLM judge → `llm_judge.json`. With `--result_dir` and no `--session_id`, judges every closed session under that directory.
- **`nika eval summary [filters] [-o PATH] [--result_dir PATH]`**: scan finished sessions, write one CSV, and print a visual summary to the terminal. See [report output](#nika-eval-summary-report-output).
- **`nika eval clean [-y] [--force]`**: delete historical artifacts under `results/`, runtime session JSON files, and the SQLite index at `runtime/sessions.db`. Refuses when running sessions exist unless **`--force`** is passed.

Typical post-benchmark flow:

```shell
nika benchmark run --config benchmark/working/cases.yaml --result_dir results/my_run
nika eval judge -p openai -m gpt-5-mini --result_dir results/my_run   # optional
nika eval summary --result_dir results/my_run
```

### `nika eval summary` filters

All filters are optional and repeatable. Omit filters to include every finished session that has the required artifacts.

| Option | Meaning |
|--------|---------|
| `-o` / `--output` | Output CSV path (default: `{result_dir}/0_summary/evaluation_summary.csv`) |
| `--result_dir` | Results parent directory to scan (default: `results/` or `nika.result_dir`) |
| `-p` / `--problem` | Root-cause / problem id (e.g. `link_down`) |
| `-e` / `--env` | Scenario / net env (e.g. `dc_clos`) |
| `-d` / `--failure-domain` | Failure domain such as `link_interface` |
| `--session_id` | Specific session id |
| `-a` / `--agent` | Agent type |
| `--model` | Agent model id |

Each finished session directory should contain at least `run.json`, `ground_truth.json`, and `eval_metrics.json`. `llm_judge.json` is optional and merged when present.

### `nika eval summary` report output

After writing the CSV, the command prints headline scores, run health, a fault-vs-healthy split, per-dimension breakdowns, and the most frequent RCA confusions.

| Option | Meaning |
|--------|---------|
| `--report` / `--no-report` | Print the terminal report (default: on). `--no-report` restores CSV-only output for CI |
| `-g` / `--group-by` | Breakdown dimension, repeatable: `domain`, `env`, `problem`, `size` (default: all four) |
| `--metric` | Metric used to sort rows and draw bars: `rca_f1` (default), `localization_f1`, `detection_score` |
| `--top` | Max rows per breakdown table; `0` shows all (default: 15) |
| `--json` | Also write the report aggregates to this JSON path |

Scoring matches `nika leaderboard pack`, so the headline here and a submission built from the same directory agree. Three consequences are worth knowing when comparing against the raw CSV:

- **`outcome=agent_failed` trials score 0.0.** Their `eval_metrics.json` holds `-1.0` sentinels; averaging the CSV column directly yields negative scores. The `outcome` CSV column distinguishes these from a genuine `0.0`.
- **Missing trials count as 0.** The denominator is `case_count x n_trials` from the result-dir run config, so trials that never landed lower the score. Applying any filter switches the denominator to the number of sessions selected.
- **Healthy (no-fault) cases are reported separately.** They have no root cause, so RCA and localization are undefined for them and only `detection_score` is scored. Breakdown tables cover fault cases only.

The same report prints automatically when `nika benchmark run` finishes.

---
---

## `nika leaderboard`

Pack, validate, and open GitHub (scores) + Hugging Face (trajectories) PRs from an official release run. See the [leaderboard submission guide](../benchmarks/leaderboard-submission.md).

```shell
nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md
nika leaderboard submit --result_dir results/my-run \
  --submission results/my-run/submission
```

`submit` packs `{result_dir}/{YYYYMMDD}_{slug}/` plus sibling `{YYYYMMDD}_{slug}_trajectories/`, validates both (unless `--skip-validate`), then opens PRs. Pack or validate failures exit before any remote submit. Requires authenticated [`gh`](https://cli.github.com/) and `HF_TOKEN`. Scores PR target: `sands-lab/nika-leaderboard` (`--repo`). Trajectories dataset PR: `Zhihao98/nika-trajectories` (`--traj-repo`). Use `--skip-github` / `--skip-trajectories` to submit only one side.

---
---

## `nika remote`

Optional control plane for splitting the agent host from the lab host. See [remote lab execution](remote.md).

- **`nika remote serve [--host 0.0.0.0] [-p 8700]`**: run the lab-host daemon. The current control plane has no shared-token option; protect it at the network boundary.
- **`nika remote health [--url URL]`**: probe `/health` (uses `nika.remote.url` from YAML when `--url` is omitted).

When `nika.remote.enabled` and `nika.remote.url` are set in `config/nika.yaml` on the agent host, `env` / `failure` / `agent` / `session` lab ops forward to the daemon transparently.

---
---

## `nika traffic`

Requires a deployed lab. By default the **current session** supplies the deployed lab name and size; override with **`--lab`** (and **`-s`** when the scenario needs a size).

- **`nika traffic list`**: supported **`TYPE`** values for `run`.
- **`nika traffic run TYPE …`**: start traffic; options depend on **`TYPE`**.

### Foreground vs background (`--background`)

| TYPE | `--no-background` (default) | `--background` |
|------|------------------------------|------------------|
| `od` | Run iperf3 clients synchronously; print JSON summaries to stdout | Start iperf3 in the background inside the lab; print a short JSON list of flow labels |
| `sndlib` | Replay each SNDlib interval synchronously | Start each interval in the background, wait `duration_sec`, then next |
| `web` | Block until interrupted or finished (`--no-loop`) | Not supported: web load blocks this CLI |

### `nika traffic fetch sndlib`

Normalize/download dynamic traffic into `.nika_cache/sndlib/traffic/<topo>/`. Requires a known adapter/URL or a hand-written normalized cache.

### `nika traffic run od`

OD-matrix iperf3 using `ODFLowGenerator`.

**Exactly one** traffic pattern:

1. **`--od-json PATH`**: JSON object `{ "src_host": { "dst_host": <rate>, ... }, ... }` (rates match `--unit`).
2. **`--mesh-mbps N`**: every ordered pair of distinct hosts in the scenario at `N` Mbit/s (with `--unit M`).
3. **`--all-to-host H --mbps N`**: every host except `H` sends to `H` at `N` Mbit/s (same pattern as incast / load-amplification examples).

Shared iperf tuning:

- **`--interval`**: iperf `-t` duration (seconds).
- **`--unit`**: `K` or `M` (bitrate suffix for matrix values).
- **`--udp` / `--no-udp`**
- **`--server-args`**, **`--client-args`**: extra iperf3 arguments.

### `nika traffic run sndlib`

Replay SNDlib demands/dynamic series on ISP stub hosts (`pc_<router>`). Deploy any `isp_<topology>` or named ISP special (`nika env run isp_abilene`, …); those labs attach stubs. Choose the matrix with **`--mode demands|dynamic`** (default `demands`) and optional **`--scale`**. Intervals play **in order**. Use **`--max-intervals N`** for smoke tests.

### `nika traffic run web`

Uses `WebBrowsingTrafficGenerator` (ApacheBench against `web_urls`). Only scenarios that define web servers and URLs are valid.

Options:

- **`--request-delay-min`**, **`--request-delay-max`**
- **`--pages-min`**, **`--pages-max`**
- **`--no-loop`**: one browsing session per client host then exit

---
---

## Helpful paths

- Runtime sessions: `runtime/sessions/*.json` (cleared when a session is finished)
- Eval summary CSV default: `results/0_summary/evaluation_summary.csv`
- Benchmark data: `benchmark/*.yaml` under the repo root
