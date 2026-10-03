# Resource profiling

`profile_scenarios.py` measures the infrastructure cost of NIKA labs without an
agent: start a lab, optionally inject and verify a benchmark failure, hold it, and
close it. It uses the sampler in `profile_resources.py` and splits
measurements into phases with the lifecycle events each session writes to
`nika.jsonl`.

Local Docker with cgroup v2 is required. Images, licenses and backend binaries must
be available as for normal NIKA runs.

## What the sampler records

`ResourceSampler` writes one line per interval (default 1 s) to
`<session_dir>/resources.jsonl`. `summarize` turns it into `resources.json`, with
per-phase and total values.

- **Lab:** the session's containers, found through the session and then read directly
  from their cgroups until they disappear, so teardown is covered. Each sample holds
  `memory.current` (including page cache), working set (`memory.current` minus
  `inactive_file`), CPU from `cpu.stat`, disk bytes from `io.stat`, `pids.current`, and
  the container count. A container's whole CPU and I/O usage is counted when it is first
  seen, because the session created it.
- **Discovery** runs in its own thread, so a slow Docker API does not delay samples.
  Kathara containers come from the session's container listing; Containerlab
  containers come from their `containerlab=<lab>` label, because `clab inspect` lists
  nodes only after deploy finishes. Discovery polls every 5 s until containers exist
  and while new ones appear, backs off to 60 s once the set is stable, and stops when
  the session is no longer running.
- **Framework:** the NIKA process tree that drives the session (the case worker and
  its children, such as `clab` or `kubectl`): CPU, including reaped children, and RSS.
  The sampler threads run in that process, so their CPU is part of the framework CPU
  and is also reported separately as `sampler_cpu_seconds`.
- **Host context:** load average, `MemAvailable`, and the CPU of `dockerd` and
  `containerd`. The Docker daemons are shared, so their CPU is context, not a per-session
  cost.
- **Link count:** recorded once in the `env_start` event from the scenario topology
  (a Kathara collision domain with two or more members is one link).

Phase windows come from existing events: deploy (`env_start`, minus the convergence
window), convergence (`env_verify`), inject (`failure_inject_complete`, including
failure verification), and cleanup (`env_stop` through `session_cleared`). A failed
start (`env_verify_failed`, `env_start_failed`) counts as deploy. Samples outside every
window fall into `other` (the hold). The phase table is `PHASES` in
`profile_resources.py`; the CSV columns follow it. Future benchmark
versions are profiled without changes as long as they emit these events.

Limits of periodic sampling:

- Short peaks can be missed, and a phase shorter than the interval can have a
  duration but no samples.
- Containers removed between two samples lose their last interval of CPU, so
  cleanup CPU inside the lab is mostly unmeasured; teardown work shows up as
  `docker_cpu_seconds` instead.
- `sampling_errors` counts failed Docker or cgroup reads while the session runs.

## Usage

```bash
# Case selection and run config are the same as `nika benchmark run` / `list`
uv run python experiment/profile/profile_scenarios.py --list
uv run python experiment/profile/profile_scenarios.py --release 0.2.0
uv run python experiment/profile/profile_scenarios.py \
  --config benchmark/working/cases.yaml --batch-size 8
# One case (task ids from `nika benchmark list`), with failure injection
uv run python experiment/profile/profile_scenarios.py --release 0.2.0 \
  --task-id <TASK_ID> --inject
```

- **Cases:** benchmark cases only, selected as in `nika benchmark run`: `--config`
  (benchmark YAML or pool), or `--release` with `--split`; without either, the
  working pool. `--task-id` (repeatable) keeps only those cases. Each distinct lab
  variant the cases deploy is profiled once, keyed by scenario, backend, size,
  `topo`, `igp`, `bgp_mode`, `rpki` and `device_profile`.
  - Add `--inject` to profile every benchmark case instead (deduplicated by case key).
    Its failure is injected and verified with the case's parameters, exactly as the
    benchmark runner does before the agent starts. Healthy cases deploy without
    injection.
- **Run config:** `--run-config` (default `config/nika.yaml`, or `NIKA_RUN_CONFIG`)
  is loaded as by `nika benchmark run`: `benchmark.batch_size`,
  `benchmark.serialize_heavy`, `nika.runtime_validation` and `nika.static_validation`
  apply. `--batch-size` and `--[no-]serialize-heavy` override the first two.
- **Session handling:** each case uses a unique test session, normal NIKA startup, a
  hold of `--hold-seconds`, and NIKA session cleanup in `finally`.
- **Admission:** at most `batch_size` cases run at once. With `serialize_heavy`,
  Containerlab, k8s/llmd/XRd and topo_size `l` cases run alone after the light cases;
  without it, every case shares the flat limit. To measure throughput at several
  concurrency levels, run once per `--batch-size`.
- **Repeats:** `--repeats N` queues every case N times, in rounds.
- **Output:** the output directory must be new. It contains `plan.json`, incremental
  `profiles.jsonl`, `profiles.csv`, `summary.csv`, and one session directory per case.
  Each session directory holds `profile.json`, `resources.json` and the normal NIKA
  artifacts. `summary.csv` reports the median, min and max of each metric over `ok`
  runs.
- **Failures and interrupts:** failures are recorded and the other cases continue; any
  failure produces a nonzero exit code. Ctrl-C stops admission, sends active workers
  SIGTERM so they clean up, and closes the remaining sessions of this run. The script
  then exits with code 130.
