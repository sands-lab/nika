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

NetOpsBench and NetArena collectors live in the separate `network-benchmark-experiments` Git repository. On xwangz-vm its checkout is `/home/ubuntu/network-benchmark-experiments`. Keep new collector outputs and upstream workspace results outside the source repository.

The author retired the historical raw data, trajectories, aggregates and figures on xwangz-vm on 2026-11-15 before restarting experiments. The old numbers remain historical placeholders in the notes and manuscript, pending new measurements.

For new runs, adapt these historical collectors to the current runtime and derive the inventory and size groups from the frozen experiment configuration. The analysis reads artifact locations from `NIKA_RESET_RESULTS`, `NETOPSBENCH_RUNS` and `NIKA_FIRECRACKER_ROOT`. Its original size tiers, experiment groups and filters describe the retired study.

Keep measurement failures and unsupported variants in new runs. The historical analysis filters some incomplete runs and must not define admission or reporting rules for a new experiment.
