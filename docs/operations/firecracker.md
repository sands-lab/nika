# Run benchmark trials in Firecracker (advanced)

Every benchmark trial normally deploys its network environment from scratch,
waits for it to converge and verify, and undeploys it afterwards. For
environments that take minutes to build, such as SR Linux topologies,
Kubernetes labs or large ISP networks, that cost repeats for every task and
trial.

With `nika.execution: firecracker`, NIKA instead works like this:

1. For each environment variant, it deploys and verifies the environment once in
   a [Firecracker](https://firecracker-microvm.github.io/) microVM and saves the
   VM (memory, device state and disk) as a *lab image*.
2. Each trial restores a disposable copy of that lab image, injects its failure,
   runs the agent, and discards the copy.

The agent and its sandbox stay on the host and reach the environment through the
NIKA remote daemon in the VM, exactly as in [remote lab execution](remote.md).

`nika.execution` defaults to `native` (labs on this host). Firecracker execution
affects only `nika benchmark run`.

## When it pays off

Measured on a 16 vCPU host that is itself a VM (nested virtualization). "Ready"
is the time until the restored lab passed NIKA's startup verification again:

| Environment | Native deploy | Firecracker, 4 KiB pages | Firecracker, huge pages |
|---|---:|---:|---:|
| `dc_clos -s s` (Kathara) | 17–19 s | 25–44 s | 4 s |
| `isp_nobel-germany` (Kathara, FRR) | not measured | 75 s | 8 s |
| `min3clos` (Containerlab, SR Linux) | 108 s | 40–100 s | 19 s |
| `k8s_lab` | 301 s | 159 s | 29 s |
| `llmd_lab` | 190 s | 231 s | 26 s |

Without huge pages, a restored copy loads memory on first access, which is slow
on nested hosts and can stall the lab's daemons. Use huge pages wherever the
host can reserve them; the default mode suits hosts that cannot, or runs that
keep many copies of one lab image at once (they share memory).

A lab image costs one native deploy plus a VM boot and the image write.
With huge pages that is 1.5–3 minutes for Kathara labs, 3–6.5 minutes for
SR Linux labs and up to 8.5 minutes for `k8s_lab`. All 29 environments of
release 0.2.0 ran in Firecracker with the same scores as native runs (mock agent),
and three copies of one lab image ran at once as fast as one.

## Set up the host (once)

These steps make the host able to run Firecracker trials and are kept across
reboots. Run them as a user with `sudo`.

1. **KVM.** Check that `/dev/kvm` exists and add yourself to its group, then log
   in again:

   ```shell
   sudo usermod -aG kvm "$USER"
   ```

2. **Nested virtualization in KVM**, for labs that run VMs themselves (vrnetlab
   images such as RouterOS). It is on by default on recent kernels; check that
   this prints `1` or `Y`:

   ```shell
   cat /sys/module/kvm_amd/parameters/nested 2>/dev/null || cat /sys/module/kvm_intel/parameters/nested
   ```

   Firecracker itself needs no installation. On first use NIKA builds
   Firecracker 1.17 with its nested virtualization patch in a Rust container,
   which takes about 3 minutes, into `<root>/firecracker/`. Upstream Firecracker
   snapshots lose the state of a hypervisor in the guest, so restored labs that
   run VMs crash without the patch. To use another binary, set
   `nika.firecracker.binary` to its name on `PATH` or its path.

3. **Firecracker root on a reflink-capable filesystem** (btrfs or XFS), owned by
   you, at `nika.firecracker.root` (default `/var/lib/nika-firecracker`). Trial disks
   are copy-on-write clones of lab image disks. Without a spare disk, use a loop
   file:

   ```shell
   sudo apt-get install -y btrfs-progs
   sudo truncate -s 300G /var/lib/nika-firecracker.img
   sudo mkfs.btrfs /var/lib/nika-firecracker.img
   sudo mkdir -p /var/lib/nika-firecracker
   echo '/var/lib/nika-firecracker.img /var/lib/nika-firecracker btrfs loop,noatime,nofail 0 0' \
     | sudo tee -a /etc/fstab
   sudo systemctl daemon-reload && sudo mount /var/lib/nika-firecracker
   sudo chown "$USER": /var/lib/nika-firecracker
   ```

   Size it for about 20 GiB of base image plus, per lab image, the memory the
   lab uses (1–3 GiB; unused memory is stored as holes) and the disk blocks its
   deploy changed. A loop file grows on the disk that holds it, and rebuilding
   the base briefly needs room for two bases. Keep that disk well below full: on
   a Kubernetes node, kubelet deletes every unused Docker image once the disk
   passes 85%.

4. **Huge pages** (recommended; see [Huge pages](#huge-pages)). Reserve them at
   boot: a running host's memory is soon too fragmented to find enough
   contiguous 2 MiB blocks. The reserved memory is unavailable to everything
   else. A copy takes huge pages only for the memory its lab uses (`nika
   firecracker list` shows it per lab image: about 1 GiB for small Kathara labs,
   2–5 GiB for ISP, k8s and SR Linux labs), but it can grow to its full
   `vm_memory`; a VM that finds the pool empty crashes, so the runner admits a
   trial only while the pool holds its full `vm_memory`. Reserve `vm_memory`
   for each copy you want to run at once. For 24 GiB:

   ```shell
   echo 'GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT} hugepagesz=2M hugepages=12288"' \
     | sudo tee /etc/default/grub.d/99-nika-hugepages.cfg
   sudo update-grub
   ```

   On a host with plenty of free memory, you can reserve them now instead of
   rebooting, and check that the count was reached. This reservation does not
   survive a reboot:

   ```shell
   sudo sysctl vm.nr_hugepages=12288
   grep HugePages_Total /proc/meminfo
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
   findmnt /var/lib/nika-firecracker        # btrfs or xfs
   ```

## Base image

Every lab image starts from a base image (Ubuntu with Docker, NIKA and this
host's runtime images, plus the vendor images it has, such as RouterOS; see
[Prepare vendor images](installation.md#prepare-vendor-images)). The first Firecracker run builds it, which takes about
20 minutes, and later runs rebuild it when this host's runtime images or Kathara
plugins change. Code changes do not: lab images receive the current checkout.

The base is assembled in Docker containers, so it needs no `sudo`. The VM has no
network of its own: while it is built, it reaches the network through a proxy on
the host, over vsock.

## Enable Firecracker runs

In `config/nika.yaml`:

```yaml
nika:
  execution: firecracker
  firecracker:
    huge_pages: true
```

Then run benchmarks as usual:

```shell
nika benchmark run --release 0.2.0 --split test
```

With Firecracker execution, every trial of the run uses a Firecracker VM, and
the runner schedules for that:

1. **Lab images first.** Before any trial starts, it builds the lab images the
   run needs and does not have yet, several at a time (bounded by vCPUs and huge
   pages). Trials then only restore, and `--case-timeout` never includes a build.
2. **One variant at a time.** Trials of the same environment variant run next to
   each other, so its lab image's memory file stays in the page cache.
3. **Admission by memory.** `--batch-size` caps concurrent trials; with huge
   pages, a trial also waits until the pool can hold its copy's whole
   `vm_memory`, so a copy can never exhaust the pool. Heavy labs (k8s, Containerlab, size `l`) no
   longer run alone: a restore costs them no more than a light lab.

### Huge pages

By default a restored copy maps the lab image's memory file and loads pages on first
access. Copies of one lab image share those pages, but the first minute after a
restore is slow, especially on hosts that are themselves VMs: lab daemons can
miss their watchdog deadlines (for example FRR's `watchfrr` exits, and the
startup verification restarts it).

With `huge_pages: true`, VM memory uses the 2 MiB huge pages reserved in
[host setup](#set-up-the-host-once) step 4, and a page server
(`nika.firecracker.uffd`) copies the memory the lab uses into each copy right after
the restore (well under a second for a small lab). Restored labs run at full
speed at once, and image builds are about twice as fast. Every running copy
needs its own huge pages for the memory its lab uses.

### VM size

Each VM gets `vm_cpus` vCPUs (default 4) and `vm_memory` (default 4 GiB), which
fits most Kathara labs. Give larger labs more with `vm_memory_overrides`; an
image build warns when a lab leaves less than a quarter of its VM memory free.
For release 0.2.0:

```yaml
vm_memory_overrides:
  k8s_lab: 8GiB
  llmd_lab: 8GiB
  min3clos: 8GiB
  isp_pdh: 20GiB
  isp_di-yuan: 20GiB
  isp_dfn-gwin: 20GiB
```

Few vCPUs per VM keep one copy's bursts (verification, injection, the agent's
commands) from starving the copies next to it.

## What changes in a trial

* A trial's results directory has the same files as a native trial, plus:
  * `image.nika.jsonl`: the image build's events;
  * `run.json` → `firecracker`: restore timings and the lab image used.
  * `env_stop` records that the lab was discarded with its VM copy;
    `failures_ended` and `session_cleared` come from the VM.
* After restore, the lab passes the same startup verification as a native deploy
  before the failure is injected. Set `verify_after_restore: false` to skip it.
* Lab-host events in `nika.jsonl` and `messages.jsonl` carry
  `"origin": "remote"` and stream to the host about once a second.
* Teardown discards the VM copy instead of undeploying the lab.
* Resume, retries, `--case-timeout` and `--batch-size` work as in native runs.
  Copies that a crashed run left behind are deleted when the same results
  directory runs again, or by `nika firecracker clean`.

### Clock

A restored VM continues on its lab image's timeline: its clock is not stepped, so
logs, leases, uptimes and protocol timers inside the lab stay consistent. As a
result, the lab clock is behind the host by the image's age. `run.json`
records the difference as `remote_clock_offset_sec`.

* Agent timing metrics use host timestamps only.
* `nika inspect` shifts lab-host rows onto the host clock, so the timeline stays
  in order; the lab's own time is kept as `lab_timestamp`.
* The log files themselves keep the lab-host time.

## Manage lab images

| Command | Effect |
|---|---|
| `nika firecracker list` | Lab images, whether trials would still use them, and trial copies (running or orphaned) |
| `nika firecracker clean` | Release orphaned trial copies; delete stale lab images (any key input changed) and abandoned builds. Builds in progress and lab images that running trials restore from are kept |
| `nika firecracker clean --all` | Also delete every lab image |

Lab images stay after a run, so later runs (other agents, more trials, reruns)
reuse them. Each takes the memory its lab used plus the disk blocks its deploy
changed, often 2–8 GiB, so a full release needs tens of GiB. To delete the ones
a run used when it ends, set:

```yaml
nika:
  firecracker:
    clean_after_run: true
```

Lab images are keyed by:

* environment variant;
* code that runs in the VM: `src/` and `pyproject.toml`, without host-only code
  (agents, LLM clients, evaluation, `nika inspect` and the TUI), so changing an
  agent keeps the lab images;
* the host's lab deploy settings (`nika.lab`, `runtime_validation`,
  `static_validation`, `k8s`), which the image deploy uses;
* base image;
* VM size and huge-page setting;
* Firecracker binary, since a lab image restores only with the binary that saved
  it.

A change to any of these builds a new lab image on the next run, and `list`
marks the old one stale.

## Limitations

* **Labs that run VMs themselves** (vrnetlab RouterOS) need KVM nested
  virtualization on the host and NIKA's Firecracker build (see
  [Set up the host](#set-up-the-host-once)). They are tested on AMD; the Intel
  (VMX) path is implemented but not yet tested.
* **The first operations after a 4 KiB-page restore run slower.** This is most
  visible on hosts that are themselves VMs; huge pages remove it.
* **Only benchmark runs use Firecracker.** `nika env run`, `nika case run` and
  task-mode `nika agent run` still deploy from scratch.
