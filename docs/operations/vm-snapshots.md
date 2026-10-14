# Run benchmark trials from VM snapshots (advanced)

Every benchmark trial normally deploys its network environment from scratch,
waits for it to converge and verify, and undeploys it afterwards. For
environments that take minutes to build, such as Kubernetes labs, SR Linux
topologies, or large ISP networks, that cost repeats for every task and trial.

With `nika.snapshot` enabled, NIKA instead works like this:

1. For each environment variant, it deploys and verifies the environment once in
   an [Incus](https://linuxcontainers.org/incus/docs/main/) VM and keeps the VM's
   memory and disk in a *golden* snapshot.
2. Each trial restores a disposable copy of that VM, injects its failure, runs the
   agent, and discards the copy.

The agent and its sandbox stay on the host and reach the environment through the
NIKA remote daemon in the VM, exactly as in [remote lab execution](remote.md).

The feature is off by default and affects only `nika benchmark run`.

## When it pays off

Measured on a 16 vCPU host. Environment time per trial covers deploy or restore,
verification, and teardown.

| Environment | Cold deploy | From snapshot | Golden build (once) |
|---|---:|---:|---:|
| `k8s_lab` | 301 s | 59 s | 658 s |
| `llmd_lab` | 190 s | 56 s | 455 s |
| `sdn_l3_clos -s l` | 294 s | 39 s | 463 s |
| `dc_clos -s l` | 277 s | 43 s | 428 s |
| ISP on Containerlab SR Linux (`isp_pdh`) | 234 s | 126 s | 704 s |
| `campus_lan -s s` | 72 s | 26 s | 183 s |
| `dc_clos -s s` | 17 s | 18 s | 110 s |

A golden build repays itself after 2–7 trials of the heavy environments above.
Small labs that converge in seconds gain nothing, so use snapshots for heavy,
large or slow-converging environments and for runs that repeat tasks (multiple
agents or trials). Full measurements:
[experiment/vm_snapshot](../../experiment/vm_snapshot/README.md).

## Prerequisites

* A Linux host with KVM (`/dev/kvm`) and enough memory for one VM per parallel
  trial. Size each VM by its environment: 8 GiB covers most labs; SR Linux ISP
  labs need about 20 GiB.
* [Incus](https://linuxcontainers.org/incus/docs/main/installing/) 6.x or later,
  initialized with:
  * a **btrfs or ZFS storage pool**, so trial copies are copy-on-write clones;
  * a managed bridge in the default profile, which the base VM uses to download
    packages and images.
* Your user in the `incus-admin` group. Log in again after adding it.

If Docker sets the host `FORWARD` policy to `DROP`, allow the Incus bridge:

```shell
sudo iptables -I DOCKER-USER -i incusbr0 -j ACCEPT
sudo iptables -I DOCKER-USER -o incusbr0 -j ACCEPT
```

## Package the base VM

Build a VM with NIKA and every benchmark image installed:

```shell
scripts/snapshot-base.sh nika-base --cpus 16 --memory 8GiB --prepare-all
```

This takes about 30 minutes, most of it preparing images. It stops if any
benchmark image is missing afterwards. The script also prepares the guest for
snapshots:

* freed memory is zeroed, so it drops out of the snapshot;
* time synchronization is disabled (see [Clock](#clock));
* the remote daemon and MCP gateway are reachable only over vsock.

It ends with the stateless snapshot `nika-base/installed`. Rebuild the base when
NIKA's Python dependencies change, then run `nika snapshot clean --all`: golden
VMs are keyed by the base's name, not its content. Code changes do not need a
rebuild: golden VMs receive the current checkout.

## Enable snapshot runs

In `config/nika.yaml`:

```yaml
nika:
  snapshot:
    enabled: true
    base: nika-base/installed
    vm_cpus: 16
    vm_memory: 8GiB
    vm_memory_overrides:
      isp_pdh: 20GiB
```

Then run benchmarks as usual:

```shell
nika benchmark run --release 0.2.0 --split test
```

The first trial of each environment variant builds its golden VM. Parallel trials
of the same variant wait for that build, then each uses its own copy. The build
counts against that trial's `--case-timeout`, which must then exceed it (up to
about 12 minutes for `k8s_lab` and SR Linux). To build all golden VMs before a
run:

```shell
nika snapshot build --release 0.2.0 --split test
```

## What changes in a trial

* A trial's results directory has the same files as a cold trial, plus:
  * `snapshot.nika.jsonl`: the golden build's events;
  * `run.json` → `snapshot`: restore timings and the golden VM used.
  * `env_stop` records that the lab was discarded with its VM copy;
    `failures_ended` and `session_cleared` come from the VM.
* After restore, the lab passes the same startup verification as a cold deploy
  before the failure is injected. Set `verify_after_restore: false` to skip it.
* Lab-host events in `nika.jsonl` and `messages.jsonl` carry
  `"origin": "remote"` and stream to the host about once a second.
* Teardown discards the VM copy instead of undeploying the lab.
* Resume, retries, `--case-timeout` and `--batch-size` work as in cold runs.
  Copies that a crashed run left behind are deleted when the same results
  directory runs again, or by `nika snapshot clean`.

### Clock

A restored VM continues on the snapshot's timeline: its clock is not stepped, so
logs, leases, uptimes and protocol timers inside the lab stay consistent. As a
result, the lab clock is behind the host by the snapshot's age. `run.json`
records the difference as `remote_clock_offset_sec`.

* Agent timing metrics use host timestamps only.
* `nika inspect` shifts lab-host rows onto the host clock, so the timeline stays
  in order; the lab's own time is kept as `lab_timestamp`.
* The log files themselves keep the lab-host time.

## Manage golden VMs

| Command | Effect |
|---|---|
| `nika snapshot list` | Golden VMs, whether trials would still use them, and trial copies (running or orphaned) |
| `nika snapshot clean` | Release orphaned trial copies; delete stale golden VMs (any key input changed) and abandoned builds; remove their build locks. Builds in progress and golden VMs that running trials restore from are kept |
| `nika snapshot clean --all` | Also delete every golden VM |

Golden VMs are keyed by:

* environment variant;
* code (`src/` and `pyproject.toml`);
* the host's lab deploy settings (`nika.lab`, `runtime_validation`,
  `static_validation`, `k8s`), which the golden deploy uses;
* base snapshot;
* VM size.

A change to any of these builds a new golden VM on the next run, and `list`
marks the old one stale. Golden VMs belong to the Incus host, not to one
checkout: `clean` deletes those of other checkouts with different code.

## Limitations

* **Restore takes about 3 s plus 4 s per GB of lab memory.** Incus stores VM
  state gzip-compressed, which bounds restore time from below.
* **The first operations after a restore run slower.** This is most visible on
  hosts that are themselves VMs.
* **Only benchmark runs use snapshots.** `nika env run`, `nika case run` and
  task-mode `nika agent run` still deploy from scratch.
