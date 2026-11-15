# Guest clock and DHCP restore experiment

These scripts reproduce the November 2026 check of DHCP leases after advancing a restored guest's wall clock. The results motivate the paper's decision to preserve the guest's clock timeline.

`restore_probe.py` clones a historical campus snapshot, captures DHCP state, advances the guest wall clock and probes addresses and connectivity. `emu/` runs a smaller DHCP clock-step control with libfaketime inside a disposable container. The scripts retain the original experiment's APIs and paths; review them before a new run. They do not implement the pending no-clock-step comparison.

The author deleted the historical logs and lease files on xwangz-vm on 2026-11-15 before restarting experiments. The Obsidian Snapshot Reset progress note records the method, limits and results. Keep outputs outside this source directory.
