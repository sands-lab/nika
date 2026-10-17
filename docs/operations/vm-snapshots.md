# Run benchmark trials from VM snapshots (advanced)

Every benchmark trial normally deploys its network environment from scratch,
waits for it to converge and verify, and undeploys it afterwards. For
environments that take minutes to build, such as SR Linux topologies,
Kubernetes labs or large ISP networks, that cost repeats for every task and
trial.

With `nika.snapshot` enabled, NIKA instead works like this:

1. For each environment variant, it deploys and verifies the environment once in
   a [Firecracker](https://firecracker-microvm.github.io/) microVM and saves the
   VM (memory, device state and disk) as a *golden* snapshot.
2. Each trial restores a disposable copy of that snapshot, injects its failure,
   runs the agent, and discards the copy.

The agent and its sandbox stay on the host and reach the environment through the
NIKA remote daemon in the VM, exactly as in [remote lab execution](remote.md).

The feature is off by default and affects only `nika benchmark run`.

## When it pays off

Measured on a 16 vCPU host that is itself a VM (nested virtualization). "Ready"
is the time until the restored lab passed NIKA's startup verification again:

| Environment | Cold deploy | From snapshot, 4 KiB pages | From snapshot, huge pages |
|---|---:|---:|---:|
| `dc_clos -s s` (Kathara) | 17–19 s | 25–44 s | 4 s |
| `isp_nobel-germany` (Kathara, FRR) | not measured | 75 s | 8 s |
| `min3clos` (Containerlab, SR Linux) | 108 s | 40–100 s | 19 s |
| `k8s_lab` | 301 s | 159 s | 29 s |
| `llmd_lab` | 190 s | 231 s | 26 s |

Without huge pages, a restored copy loads memory on first access, which is slow
on nested hosts and can stall the lab's daemons. Use huge pages wherever the
host can reserve them; the default mode suits hosts that cannot, or runs that
keep many copies of one golden VM at once (they share memory).

A golden snapshot costs one cold deploy plus a VM boot and the snapshot write.
With huge pages that is 1.5–3 minutes for Kathara labs, 3–6.5 minutes for
SR Linux labs and up to 8.5 minutes for `k8s_lab`. All 29 environments of
release 0.2.0 ran from snapshots with the same scores as cold runs (mock agent),
and three copies of one golden VM ran at once as fast as one.

## Set up the host (once)

These steps make the host able to run snapshot trials and are kept across
reboots. Run them as a user with `sudo`.

1. **KVM.** Check that `/dev/kvm` exists and add yourself to its group, then log
   in again:

   ```shell
   sudo usermod -aG kvm "$USER"
   ```

2. **Firecracker** 1.17 or later on `PATH` (or set `nika.snapshot.firecracker`):

   ```shell
   v=v1.17.0; arch=$(uname -m)
   curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/$v/firecracker-$v-$arch.tgz" \
     | tar -xz -C /tmp
   sudo install -m 755 "/tmp/release-$v-$arch/firecracker-$v-$arch" /usr/local/bin/firecracker
   ```

3. **Snapshot root on a reflink-capable filesystem** (btrfs or XFS), owned by
   you, at `nika.snapshot.root` (default `/var/lib/nika-snapshots`). Trial disks
   are copy-on-write clones of golden disks. Without a spare disk, use a loop
   file:

   ```shell
   sudo truncate -s 300G /var/lib/nika-snapshots.img
   sudo mkfs.btrfs /var/lib/nika-snapshots.img
   sudo mkdir -p /var/lib/nika-snapshots
   echo '/var/lib/nika-snapshots.img /var/lib/nika-snapshots btrfs loop,noatime,nofail 0 0' \
     | sudo tee -a /etc/fstab
   sudo systemctl daemon-reload && sudo mount /var/lib/nika-snapshots
   sudo chown "$USER": /var/lib/nika-snapshots
   ```

   Size it for about 20 GiB of base image plus, per golden VM, the memory the
   lab uses (1–3 GiB; unused memory is stored as holes) and the disk blocks its
   deploy changed.

4. **Huge pages** (recommended; see [Huge pages](#huge-pages)). Reserve them at
   boot: a running host's memory is soon too fragmented to find enough
   contiguous 2 MiB blocks. Reserve `vm_memory / 2 MiB` pages per copy you run
   at once; the reserved memory is unavailable to everything else. For three
   8 GiB copies (24 GiB):

   ```shell
   echo 'GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} hugepagesz=2M hugepages=12288"' \
     | sudo tee /etc/default/grub.d/99-nika-hugepages.cfg
   sudo update-grub
   ```

5. **userfaultfd for the `kvm` group**, used by the huge-page page server:

   ```shell
   echo 'KERNEL=="userfaultfd", GROUP="kvm", MODE="0660"' \
     | sudo tee /etc/udev/rules.d/99-nika-userfaultfd.rules
   sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=userfaultfd
   ```

6. **Runtime images, k8s caches and the Kathara network plugin** on the host.
   The base image copies the images, the plugin and `.nika_cache/k8s-images`
   and `.nika_cache/helm` into the VM, which has no network of its own:

   ```shell
   nika images prepare
   docker plugin install --grant-all-permissions kathara/katharanp_vde:$(dpkg --print-architecture)
   ```

7. **Reboot** (for the huge pages), then check:

   ```shell
   grep HugePages_Total /proc/meminfo     # the reserved count
   ls -l /dev/userfaultfd                 # crw-rw---- root kvm
   findmnt /var/lib/nika-snapshots        # btrfs or xfs
   firecracker --version
   ```

## Package the base image

Build the base image once (about 15 minutes; needs `sudo` for the chroot):

```shell
scripts/snapshot-base.sh
```

It unpacks Ubuntu 24.04 into an ext4 image, installs the kernel, Docker and the
guest services, then boots it in Firecracker to run `scripts/install.sh` and
load this host's runtime images. The VM has no network of its own: while it is
built, it reaches the network through a proxy on the host, over vsock.

Rebuild the base when NIKA's Python dependencies or the runtime images change.
Code changes do not need a rebuild: golden VMs receive the current checkout.

## Enable snapshot runs

In `config/nika.yaml`:

```yaml
nika:
  snapshot:
    enabled: true
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
counts against that trial's `--case-timeout`. To build all golden VMs before a
run:

```shell
nika snapshot build --release 0.2.0 --split test
```

### Huge pages

By default a restored copy maps the golden memory file and loads pages on first
access. Copies of one golden share those pages, but the first minute after a
restore is slow, especially on hosts that are themselves VMs: lab daemons can
miss their watchdog deadlines (for example FRR's `watchfrr` exits, and the
startup verification restarts it).

With `huge_pages: true`, VM memory uses the 2 MiB huge pages reserved in
[host setup](#set-up-the-host-once) step 4, and a page server
(`nika.snapshot.uffd`) copies the whole memory into each copy right after the
restore (about 2 s for 10 GiB). Restored labs run at full speed at once, and
golden builds are about twice as fast; every running copy needs its own
`vm_memory` of huge pages.

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
| `nika snapshot clean` | Release orphaned trial copies; delete stale golden VMs (any key input changed) and abandoned builds. Builds in progress and golden VMs that running trials restore from are kept |
| `nika snapshot clean --all` | Also delete every golden VM |

Golden VMs are keyed by:

* environment variant;
* code (`src/` and `pyproject.toml`);
* the host's lab deploy settings (`nika.lab`, `runtime_validation`,
  `static_validation`, `k8s`), which the golden deploy uses;
* base image;
* VM size and huge-page setting.

A change to any of these builds a new golden VM on the next run, and `list`
marks the old one stale.

## Limitations

* **Labs that run VMs themselves** (vrnetlab images such as RouterOS) do not work:
  Firecracker offers no nested virtualization to its guests.
* **The first operations after a 4 KiB-page restore run slower.** This is most
  visible on hosts that are themselves VMs; huge pages remove it.
* **Only benchmark runs use snapshots.** `nika env run`, `nika case run` and
  task-mode `nika agent run` still deploy from scratch.
