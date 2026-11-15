# Snapshot reset timing

This directory preserves the NIKA collectors and combined analysis for the October 2026 reset-cost experiment. The historical runs used `b7717d65` plus the startup-address parsing fix. These scripts use historical runtime APIs and task sizes; adapt them before measuring a new release.

| Files | Purpose |
|---|---|
| `inplace.py`, `tasks.yaml` | Deploy each selected variant, inject a fault, recover it and re-verify |
| `native.yaml`, `fc_lab.yaml`, `fc.yaml` | Historical mock-agent execution settings |
| `probe/` | Startup phase instrumentation through `sitecustomize` |
| `parse.py` | Parse native or Firecracker trial phase times |
| `analyze.py` | Combine NIKA and external benchmark timings into `phases.csv` |
| `setup_breakdown.py`, `make_figs.py` | Historical setup analysis and figures |

NetOpsBench and NetArena collectors live in the separate `network-benchmark-experiments` Git repository. On xwangz-vm its checkout is `/home/ubuntu/network-benchmark-experiments`. Collector outputs and their upstream workspace results remain outside the source repository.

To reanalyze the historical artifacts, set their locations explicitly:

```sh
export NIKA_RESET_RESULTS=/path/to/reset-exp
export NETOPSBENCH_RUNS=/path/to/NetOpsBench/.netopsbench/runs
export NIKA_FIRECRACKER_ROOT=/path/to/firecracker-metadata
uv run python experiment/snapshot_reset/analyze.py
uv run --with matplotlib python experiment/snapshot_reset/setup_breakdown.py
uv run --with matplotlib python experiment/snapshot_reset/make_figs.py
```

The analysis uses historical size tiers and fixed experiment groups. Snapshot build metadata has a separate retention archive on xwangz-vm. Existing `phases.csv` and `setup.csv` preserve the historical aggregates. The analysis may omit image-to-variant mappings if you supply a metadata root without the original image records.

Keep measurement failures and unsupported variants in new runs. The historical analysis filters some incomplete runs and must not define admission or reporting rules for a new experiment.
