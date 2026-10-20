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

Firecracker execution makes each trial cheaper in exchange for a one-time
build per environment variant and the disk to keep the result. A lab image
build costs about one native deploy and holds 1–15 GiB; after that, a trial
restores its lab in seconds instead of deploying and undeploying it.

For release 0.2.0 (test split, 85 tasks × 3 trials), measured on a 16 vCPU host
that is itself a VM, with huge pages:

| | Native | Lab images | Lab and case images |
|---|---:|---:|---:|
| One-time builds (VM time) | — | 1.5 h | 1.5 h + 2.0 h |
| Disk | — | 21 GiB base + 79 GiB | 21 GiB base + 79 GiB + 4.4 GiB |
| Time outside the agent per run (255 trials) | 10.8 h | 0.9 h | 0.1 h |
| Builds pay for themselves after | — | the first run | about 2.4 more runs |
| Measured wall time, 85 trials 8 at a time | 5 h 55 min | 1 h 55 min, plus 35 min of builds | — |

*Time outside the agent* covers deploy or restore, verification, injection and
teardown; agent time is the same in all modes. A *run* is every task of the
split with 3 trials each. [Case images](#case-images-optional) are optional and
pay off only when the same tasks run again.

Native runs also take longer in wall time because heavy labs (k8s, Containerlab,
size `l`) run alone; Firecracker trials run side by side. With 6–8 copies at
once, tools that probe the network (ping meshes, HTTP tests) took 20–150%
longer in their median than in the native run, which ran fewer trials at once.

While a lab image is built, its snapshot temporarily needs the VM's whole
`vm_memory` (4–20 GiB) of free disk. See [host setup](#set-up-the-host-once)
step 3 for sizing the Firecracker root.

### Per environment

*Per trial* is the median time outside the agent. *Pays off after* is the number
of runs of that environment's tasks after which its builds have cost less than
the time they saved. Case images count failure tasks only; healthy tasks
restore the lab image.

| Environment | Tasks | Native, per trial | Lab image: build, disk | Firecracker, per trial | Pays off after (runs) | Case images: builds, disk | With case images, per trial | Case images pay off after (more runs) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `campus_lan` l | 6 | 196 s | 241 s, 3.4 GiB | 27 s | 0.08 | 702 s, 641 MiB | 2 s | 1.6 |
| `campus_lan` m | 6 | 103 s | 154 s, 2.0 GiB | 5 s | 0.09 | 280 s, 150 MiB | 2 s | 5.8 |
| `campus_lan` s | 4 | 87 s | 106 s, 1.7 GiB | 10 s | 0.12 | 267 s, 89 MiB | 2 s | 2.7 |
| `dc_clos` l | 2 | 296 s | 280 s, 3.3 GiB | 11 s | 0.16 | 98 s, 67 MiB | 2 s | 3.3 |
| `dc_clos` m | 3 | 67 s | 91 s, 1.7 GiB | 7 s | 0.17 | 151 s, 62 MiB | 2 s | 3.8 |
| `dc_clos` s | 2 | 36 s | 61 s, 1.1 GiB | 3 s | 0.31 | 139 s, 101 MiB | 3 s | 0.5 |
| `enterprise_branch` l | 5 | 81 s | 169 s, 2.1 GiB | 6 s | 0.15 | 388 s, 214 MiB | 2 s | 5.9 |
| `enterprise_branch` m | 4 | 64 s | 89 s, 1.4 GiB | 11 s | 0.14 | 318 s, 100 MiB | 2 s | 2.3 |
| `enterprise_branch` s | 3 | 43 s | 61 s, 1.2 GiB | 9 s | 0.20 | 133 s, 57 MiB | 2 s | 2.1 |
| `isp_abilene` s | 1 | 80 s | 152 s, 1.6 GiB | 6 s | 0.69 | 66 s, 47 MiB | 2 s | 5.3 |
| `isp_dfn-bwin_ebgp_rtbh` s | 1 | 97 s | 113 s, 1.5 GiB | 6 s | 0.41 | 40 s, 43 MiB | 3 s | 4.5 |
| `isp_di-yuan` s | 1 | 92 s | 145 s, 1.8 GiB | 18 s | 0.66 | 171 s, 49 MiB | 2 s | 3.4 |
| `isp_geant_ebgp_rpki` m | 2 | 640 s | 661 s, 2.1 GiB | 7 s | 0.17 | 201 s, 114 MiB | 2 s | 6.4 |
| `isp_germany50` l | 1 | 111 s | 126 s, 2.8 GiB | 12 s | 0.42 | 141 s, 94 MiB | 2 s | 4.7 |
| `isp_pdh` s | 1 | 237 s | 574 s, 14.8 GiB | 117 s | 1.59 | 449 s, 206 MiB | 5 s | 1.3 |
| `k8s_lab` | 6 | 344 s | 460 s, 7.7 GiB | 23 s | 0.08 | 924 s, 683 MiB | 2 s | 2.8 |
| `llmd_lab` | 5 | 207 s | 258 s, 7.7 GiB | 20 s | 0.09 | 859 s, 533 MiB | 3 s | 3.2 |
| `min3clos` | 1 | 115 s | 207 s, 5.4 GiB | 10 s | 0.66 | 151 s, 65 MiB | 3 s | 7.2 |
| `p4_dc_fabric` l | 1 | 187 s | 192 s, 1.9 GiB | 9 s | 0.36 | 56 s, 39 MiB | 2 s | 2.8 |
| `p4_dc_fabric` m | 1 | 106 s | 123 s, 1.4 GiB | 12 s | 0.44 | 48 s, 27 MiB | 5 s | 2.2 |
| `p4_dc_gateway` l | 5 | 173 s | 193 s, 2.2 GiB | 17 s | 0.08 | 350 s, 220 MiB | 3 s | 1.7 |
| `p4_dc_gateway` m | 7 | 88 s | 118 s, 1.6 GiB | 8 s | 0.07 | 281 s, 161 MiB | 2 s | 2.7 |
| `p4_dc_gateway` s | 7 | 70 s | 89 s, 1.3 GiB | 11 s | 0.07 | 391 s, 151 MiB | 3 s | 2.2 |
| `sdn_l3_clos` l | 4 | 298 s | 283 s, 2.9 GiB | 8 s | 0.08 | 237 s, 307 MiB | 2 s | 4.0 |
| `sdn_l3_clos` m | 2 | 130 s | 168 s, 2.2 GiB | 4 s | 0.22 | 50 s, 44 MiB | 2 s | 6.4 |
| `sdn_l3_clos` s | 4 | 82 s | 114 s, 1.9 GiB | 10 s | 0.13 | 248 s, 242 MiB | 2 s | 2.4 |

Lab images pay off within the first run for every environment except
`isp_pdh`, whose SR Linux lab takes 15 GiB and longer to build than to deploy.
Case images pay off after 0.5–7 more runs, depending on how long injection takes.

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

   Size it for the base image (about 21 GiB) plus the lab images you keep: each
   holds the memory its lab uses, 1–15 GiB, and release 0.2.0's test split
   needs 79 GiB ([per environment](#per-environment)). Add free space for
   builds: a snapshot first writes the VM's whole `vm_memory` (4–20 GiB), and
   NIKA starts a build only while the root has room for every running one.
   Rebuilding the base briefly needs room for two bases.

   `truncate` does not check that the disk holding the loop file has the space,
   and btrfs on a loop file stops with I/O errors when that disk fills up. Size
   the file to the real free space of that disk, which also holds NIKA's runtime
   images (about 20 GiB). On a Kubernetes node, keep the disk well below 85%:
   kubelet then deletes every unused Docker image.

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
   docker plugin ls | grep -q katharanp \
     || docker plugin install --grant-all-permissions kathara/katharanp_vde:$(dpkg --print-architecture)
   ```

   NIKA's installer already installs the plugin.

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
   pages). Trials then only restore, and `--case-timeout` does not include a
   build. If a build fails, each trial of that variant retries it within its own
   case timeout.
2. **One variant at a time.** Trials of the same environment variant run next to
   each other, so its lab image's memory file stays in the page cache.
3. **Admission by memory.** `--batch-size` caps concurrent trials. With huge
   pages, a trial also waits until the huge pages free at the start of the run
   can hold its copy's whole `vm_memory`, so a running copy never finds the pool
   empty. Heavy labs (k8s, Containerlab, size `l`) no longer run alone: a
   restore costs them no more than a light lab.

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
speed at once: `dc_clos -s s` passes its verification again 4 s after a restore,
against 25–44 s without huge pages, and image builds are about twice as fast.
Every running copy needs its own huge pages for the memory its lab uses.

### VM size

Each VM gets `vm_cpus` vCPUs (default 4) and `vm_memory` (default 4 GiB), which
fits most Kathara labs. Give larger labs more with `vm_memory_overrides`; an
image build warns when a lab leaves less than a quarter of its VM memory free.
For release 0.2.0, plus Cisco XRd (`iosxr_simple_bgp`), whose build otherwise
runs out of memory:

```yaml
vm_memory_overrides:
  k8s_lab: 8GiB
  llmd_lab: 8GiB
  min3clos: 8GiB
  iosxr_simple_bgp: 8GiB
  isp_pdh: 20GiB
  isp_di-yuan: 20GiB
  isp_dfn-gwin: 20GiB
```

Few vCPUs per VM keep one copy's bursts (verification, injection, the agent's
commands) from starving the copies next to it.

### Case images (optional)

A trial restored from a lab image still verifies the environment and injects
its failure before the agent starts. For most tasks that takes a few seconds,
but in release 0.2.0 injection takes 50–110 s for several `dc_clos`,
`enterprise_branch`, `campus_lan`, `k8s_lab` and `llmd_lab` tasks, and
verifying `isp_pdh` (SR Linux) takes about 100 s, again in every trial.

To skip that when you run the same tasks repeatedly, such as several agents or
models on one release, keep each task's VM right after injection as a *case
image*:

```yaml
nika:
  firecracker:
    case_images: true
```

Every trial of the task then restores its case image, and the agent starts 1–8 s
after the trial begins. A case image stores only the memory pages that changed
since the lab image, compressed, plus the disk blocks that changed: median
40 MiB, up to 190 MiB, and 4.4 GiB for release 0.2.0's test split. Building one
takes a restore, the injection and a snapshot, about a minute for most tasks.
[Per environment](#per-environment) lists the build time, disk and break-even
point.

Case images change how trials start:

* All trials of a task start from the same injected state instead of injecting
  the failure again. Failures that evolve over time (traffic floods, resource
  contention, flapping) continue from the moment of injection.
* A trial checks that the injected failure is still present instead of
  repeating the startup verification, and fails if it is not.
* Healthy tasks restore the lab image as before.

The restore times above assume the lab image's memory is in the page cache,
which is usual because trials of one environment variant run together. The first
restore of a variant reads that memory from disk: 10–40 s for most labs and
about 100 s for `isp_pdh` (15 GiB). Without huge pages, copies of a case image
receive their memory from the page server instead of sharing the lab image
file, so each copy holds the memory its lab uses (1–8 GiB).

## What changes in a trial

* A trial's results directory has the same files as a native trial, plus:
  * `image.nika.jsonl`: the image build's events;
  * `run.json` → `firecracker`: restore timings and the lab image used.
  * `env_stop` records that the lab was discarded with its VM copy;
    `failures_ended` and `session_cleared` come from the VM.
* After restore, the lab passes the same startup verification as a native deploy
  before the failure is injected. Set `verify_after_restore: false` to skip it.
  [Case images](#case-images-optional) check the injected failure instead.
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
| `nika firecracker list` | Lab and case images, whether trials would still use them, and trial copies (running or orphaned) |
| `nika firecracker clean` | Release orphaned trial copies; delete stale lab and case images (any key input changed) and abandoned builds. Builds in progress and images that running trials restore from are kept |
| `nika firecracker clean --all` | Also delete every lab and case image |

Lab and case images stay after a run, so later runs (other agents, more trials,
reruns) reuse them; [Per environment](#per-environment) lists the disk each one
takes. To delete the ones a run used when it ends, set:

```yaml
nika:
  firecracker:
    clean_after_run: true
```

Lab images are keyed by:

* environment variant;
* code that runs in the VM: `src/` and `pyproject.toml`, without host-only code
  (agents and their sandboxes, LLM clients, evaluation, `nika inspect`, the
  TUI and NIKA's Firecracker host code), so changing an agent keeps the lab images;
* the host's lab deploy settings (`nika.lab`, `runtime_validation`,
  `static_validation`, `k8s`), which the image deploy uses;
* base image;
* VM size and huge-page setting;
* Firecracker binary, since a lab image restores only with the binary that saved
  it.

A change to any of these builds a new lab image on the next run, and `list`
marks the old one stale. A case image is keyed by its lab image and its task, so
a new lab image also builds new case images.

Stale images are not deleted automatically, because another checkout may still
use them. Run `nika firecracker clean` after updating NIKA. A build that would
leave the Firecracker root without room for its snapshot fails at once and asks
you to clean up.

## Limitations

* **Labs that run VMs themselves** (vrnetlab RouterOS) need KVM nested
  virtualization and NIKA's Firecracker build (see
  [Set up the host](#set-up-the-host-once)). Run NIKA on bare metal or in a VM,
  not in a VM inside another VM. There the router VM would run four levels deep,
  and each level multiplies the cost of its VM exits (about 13× per level,
  measured on an AMD host whose hypervisor exposes only `npt` and `nrip_save`).
  The router VM then spends all its time on timer interrupts, never leaves its
  BIOS, and verification fails. Labs without inner VMs work at any depth. These
  labs are tested on AMD; the Intel (VMX) path is implemented but not tested.
* **The first operations after a 4 KiB-page restore run slower.** This is most
  visible on hosts that are themselves VMs; huge pages remove it.
* **Only benchmark runs use Firecracker.** `nika env run`, `nika case run` and
  task-mode `nika agent run` still deploy from scratch.
