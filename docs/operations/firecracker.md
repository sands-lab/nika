# Run benchmark trials in Firecracker (advanced)

Each benchmark trial normally deploys its network environment, waits for it to converge and verify, and undeploys it afterwards. For environments that take minutes to build, such as SR Linux topologies, Kubernetes labs, or large ISP networks, that cost repeats for every task and trial.

Firecracker execution removes the repeated cost. NIKA deploys and verifies each environment variant once inside a [Firecracker](https://firecracker-microvm.github.io/) microVM, and saves the VM as a *lab image*. Each trial then restores a disposable copy of that image, injects its failure, runs the agent, and discards the copy. The agent and its sandbox stay on the host and reach the environment through the NIKA remote daemon in the VM, as in [remote lab execution](remote.md).

Set `nika.execution.mode` to `firecracker` to use it. The default is `native`, which deploys labs on this host. Firecracker execution affects only `nika benchmark run`.

## When to use it

Use Firecracker execution when you run the same tasks more than once on a host, such as several agents or models, or when the environments take minutes to deploy. On release 0.2.0's test split, measured before the release pinned its engine, the time trials spend outside the agent dropped from 10.8 h to 0.9 h per run. Restored labs give the same scores as native ones with the mock agent. With a real agent, scores differed on some tasks in both directions (see [Compatibility and scores](#compatibility-and-scores)).

Stay with native execution for a single run of a few tasks, where each environment would be deployed once anyway, or on a host without KVM, enough disk, or enough memory to reserve as huge pages. [Performance reference](#performance-reference) has the measurements per environment, including build time, disk use, and break-even point.

Firecracker execution runs working YAML matrices and releases that do not pin a [benchmark engine](../../GLOSSARY.md#benchmarks). It cannot run a release that pins one yet, such as `--release 0.2.0`. To run release 0.2.0's tasks, pass its task file as a matrix, for example `--config benchmark/releases/0.2.0/test.yaml`. Such a run uses the current NIKA code instead of the release's engine, so it is not an official release run.

Firecracker execution needs:

* Linux on x86-64 with KVM, on bare metal or in a VM. Labs that run VMs themselves (RouterOS) do not work in a VM inside another VM.
* A btrfs filesystem for the images: about 35 GiB for release 0.2.0's test split, plus up to 20 GiB free while images are built. XFS also works, without compression, at about 3.5 times the size.
* Memory to reserve as huge pages. The installer reserves half of the host's memory, which other applications can then no longer use (see [Huge pages and disk](#huge-pages-and-disk)).

## Set up the host (once)

Run the installer with `--with-firecracker` from the NIKA checkout, as a user with `sudo`:

```shell
./scripts/install.sh --with-firecracker
```

The installer ends with a list of the steps left to you, typically a new login shell and sometimes a reboot. After those, run benchmarks as usual (see [Run benchmarks](#run-benchmarks)).

The installer makes these changes, which persist across reboots:

| Setting | What the installer does |
|---|---|
| KVM access | Adds you to the group that owns `/dev/kvm`. |
| Huge pages | Reserves half of the host's memory as 2 MiB huge pages, now and at boot (`/etc/sysctl.d/99-nika-hugepages.conf`), or keeps a larger reservation the host already has. |
| userfaultfd | Gives the `kvm` group access to `/dev/userfaultfd` (`/etc/udev/rules.d/99-nika-userfaultfd.rules`), which the restore process needs. |
| Firecracker root | Mounts a btrfs loop file, `runtime/firecracker.img`, at `runtime/firecracker` through `/etc/fstab`, unless the root is already on btrfs or XFS. It installs `btrfs-progs` with apt when `mkfs.btrfs` is missing. |
| Kathara network plugin | Installs it in the host's Docker. Lab images receive it too, because the VM has no network of its own. |
| Run config | Sets `nika.execution.mode: firecracker` in `config/nika.yaml`. |

[Uninstalling NIKA](installation.md#uninstall) unmounts and deletes the loop file and removes the huge-page and userfaultfd settings, restoring the earlier huge-page reservation. Your `kvm` group membership and `config/nika.yaml` stay.

Firecracker itself needs no separate installation. On first use, NIKA builds Firecracker 1.17 in a Rust container, which takes about 3 minutes. NIKA's build includes a nested-virtualization patch. Without it, restored labs that run VMs (vrnetlab images such as RouterOS) crash. Those labs also need KVM nested virtualization. The installer checks for it and tells you when it is off.

### Huge pages and disk

Every running copy holds its VM memory in huge pages, and the reserved memory is unavailable to everything else. A copy uses huge pages only for the memory its lab uses. `nika firecracker list` shows that per lab image: about 1 GiB for small Kathara labs, and 2 to 5 GiB for ISP, Kubernetes, and SR Linux labs. A copy can grow to its full `vm_memory`, and a VM that finds the pool empty crashes. The runner therefore admits a trial only while the pool holds its full `vm_memory`. Half of the host's memory runs several copies at once. The largest labs of release 0.2.0 need 20 GiB for one. To change the reservation, edit `vm.nr_hugepages` (in 2 MiB pages) in `/etc/sysctl.d/99-nika-hugepages.conf` and reboot.

The installer frees the page cache and compacts memory before it reserves the pages. When the host's memory is too fragmented to find enough contiguous 2 MiB blocks, it asks you to reboot. The reservation then happens at boot.

NIKA keeps the base image, lab and case images, trial copies, and its Firecracker build in `runtime/firecracker/` in the NIKA checkout. Trial disks are copy-on-write clones of lab image disks, which needs btrfs or XFS. On btrfs, NIKA stores images zstd-compressed. The installer sizes the loop file to the free space of the disk that holds `runtime/`, minus 20 GiB for Docker images, up to 150 GiB. It stops when that leaves less than 60 GiB. A loop file that outgrows the real free space stops with I/O errors. On a Kubernetes node, keep the disk well below 85%, because kubelet then deletes every unused Docker image.

The root needs room for the base image (about 12 GiB) plus the lab images you keep. Each lab image holds the memory its lab uses, compressed to 0.3 to 4 GiB. Release 0.2.0's test split needs 23 GiB of lab images ([per environment](#per-environment)). NIKA starts a build only while the root has room for the VM's whole `vm_memory` per running build: 4 to 20 GiB for release 0.2.0, and more for RouterOS and IOS XR labs (see [VM size](#vm-size)). Rebuilding the base briefly needs room for two bases.

To keep the images elsewhere, such as on a separate disk, set `nika.execution.firecracker.root` to a directory on btrfs or XFS before you run the installer.

## Run benchmarks

Run benchmarks as usual:

```shell
nika benchmark run --config benchmark/working/cases.yaml
```

With Firecracker execution, every trial of the run uses a Firecracker VM, and the runner schedules them as follows:

1. **Lab images first.** Before any trial starts, the runner builds the lab images the run needs and does not have yet, several at a time, bounded by vCPUs and huge pages. Trials then only restore, and `--case-timeout` does not include a build. If a build fails, each trial of that variant retries it within its own case timeout.
2. **One variant at a time.** Trials of the same environment variant run next to each other, so the lab image's memory stays in the page cache.
3. **Admission by memory.** `--batch-size` caps concurrent trials. A trial also waits until the huge pages free at the start of the run can hold its copy's whole `vm_memory`, so a running copy never finds the pool empty. Heavy labs (Kubernetes, Containerlab, size `l`) no longer run alone, because a restore costs them no more than a light lab.

### VM size

Each VM gets `vm_cpus` vCPUs (default 4) and `vm_memory` (default 4 GiB), which fits most Kathara labs. A variant whose routers run a router platform (`device_profile` SR Linux, SONiC, IOS XR, or RouterOS) is sized from what it deploys. Memory is 1 GiB for the guest, plus the platform's memory per router (SONiC 256 MiB, RouterOS 1 GiB, SR Linux 1.5 GiB, IOS XR 3 GiB), plus 64 MiB per other node, rounded up to whole GiB. vCPUs are 1 for the guest, plus the platform's CPU per router (SONiC 250 millicores; SR Linux, RouterOS, and IOS XR 1 vCPU), rounded up, and never below the default `vm_cpus`. `isp_pdh` gets 4 vCPUs and 5 GiB on SONiC, and 12 vCPUs and 19 GiB on SR Linux.

Other large labs get 8 GiB by default: `dc_clos`, `k8s_lab`, `llmd_lab`, `min3clos`, and Cisco XRd (`iosxr_simple_bgp`). That 8 GiB is also a floor when a router platform would otherwise size the VM smaller, so `min3clos` stays at 8 GiB on SONiC and SR Linux. `isp_pdh`, `isp_di-yuan`, and `isp_dfn-gwin` get 12 vCPUs when they have no router platform. `dc_clos` size `l` uses about 4.2 GiB in the guest. An image build warns when a lab leaves less than a quarter of its VM memory free. In that case, raise `vm_memory_overrides` for the environment, or for one variant with `<scenario>@<device_profile>`:

```yaml
nika:
  execution:
    firecracker:
      vm_memory_overrides:
        k8s_lab: 12GiB
        isp_pdh@sonic: 6GiB
```

Use `vm_cpus_overrides` the same way for vCPUs (`isp_pdh: 16` or `isp_pdh@sonic: 6`). Both override maps take precedence over the sizes above. Few vCPUs per VM keep one copy's bursts, such as verification, injection, and the agent's commands, from starving the copies next to it.

### Case images (optional)

A trial restored from a lab image still verifies the environment and injects its failure before the agent starts. For most tasks that takes a few seconds. In release 0.2.0, however, injection takes 50 to 110 s for several `dc_clos`, `enterprise_branch`, `campus_lan`, `k8s_lab`, and `llmd_lab` tasks, and verifying `isp_pdh` (SR Linux) takes tens of seconds. Trials repeat that work every time.

To skip it when you run the same tasks repeatedly, such as several agents or models on one release, keep each task's VM right after injection as a *case image*:

```yaml
nika:
  execution:
    firecracker:
      case_images: true
```

Every trial of the task then restores its case image, and the agent starts 1 to 8 s after the trial begins. A case image stores only the memory pages that changed since the lab image, compressed, plus the disk blocks that changed. A case image is median 32 MiB, up to 180 MiB, and 3.5 GiB for release 0.2.0's test split. Building one takes a restore, the injection, and a snapshot, about a minute for most tasks. [Per environment](#per-environment) lists the build time, disk use, and break-even point.

Case images change how trials start:

* All trials of a task start from the same injected state instead of injecting the failure again. Failures that evolve over time, such as traffic floods, resource contention, or flapping, continue from the moment of injection.
* A trial checks that the injected failure is still present instead of repeating the startup verification, and fails if it is not.
* Healthy tasks restore the lab image as before.

The restore times above assume the lab image's memory is in the page cache, which is usual because trials of one environment variant run together. The first restore of a variant reads that memory from disk: 10 to 40 s for most labs, and about 100 s for `isp_pdh` (15 GiB).

## Manage lab images

| Command | Effect |
|---|---|
| `nika firecracker list` | Lists lab and case images, whether trials would still use them, and trial copies (running or orphaned). |
| `nika firecracker clean` | Releases orphaned trial copies, and deletes stale lab and case images (any key input changed) and abandoned builds. Builds in progress and images that running trials restore from are kept. |
| `nika firecracker clean --all` | Also deletes every lab and case image. |

Lab and case images stay after a run, so later runs (other agents, more trials, reruns) reuse them. [Per environment](#per-environment) lists the disk each one takes. To delete the images a run used when it ends, set:

```yaml
nika:
  execution:
    firecracker:
      clean_after_run: true
```

Stale images are not deleted automatically, because another checkout may still use them. Run `nika firecracker clean` after you update NIKA. A build that would leave the Firecracker root without room for its snapshot fails at once and asks you to clean up. The key inputs of a lab image are listed in [Lab image keys](#lab-image-keys).

## Limitations

* **Labs that run VMs themselves** (vrnetlab RouterOS) need KVM nested virtualization and NIKA's Firecracker build (see [Set up the host](#set-up-the-host-once)). Run NIKA on bare metal or in a VM, not in a VM inside another VM. There the router VM would run four levels deep, and each level multiplies the cost of its VM exits (about 13 times per level, measured on an AMD host whose hypervisor exposes only `npt` and `nrip_save`). The router VM then spends its time on timer interrupts, never leaves its BIOS, and verification fails. Labs without inner VMs work at any depth. These labs are tested on AMD. The Intel (VMX) path is implemented but not tested.
* **Huge pages are required.** Without them (`huge_pages: false`), restored labs run slowly for their first minute, and much longer when several case images restore at once (see [Huge pages](#huge-pages)).
* **Only benchmark runs use Firecracker.** `nika env run`, `nika case run`, and task-mode `nika agent run` still deploy from scratch.

---

The rest of this page is reference material: what a trial records, how lab images are keyed, and the measurements behind the recommendations above.

## Huge pages

Firecracker execution needs huge pages (`huge_pages: true`, the default). VM memory uses the 2 MiB huge pages reserved in [host setup](#set-up-the-host-once). A page server copies the memory the lab uses into each copy right after the restore, which takes well under a second for a small lab. Restored labs then run at full speed at once: `dc_clos -s s` passes its verification again 4 s after a restore, against 25 to 44 s without huge pages, and image builds are about twice as fast. Every running copy needs its own huge pages for the memory its lab uses.

`huge_pages: false` remains for experiments only. Copies then run on 4 KiB pages, and every page a lab touches after a restore costs a separate fault, which is expensive on hosts that are themselves VMs. Lab daemons can miss their watchdog deadlines (for example, FRR's `watchfrr` exits). With case images, where each copy receives its own memory from the page server, a `docker exec` in the lab takes 7 to 9 s while several copies restore at once.

## Base image

Every lab image starts from a base image: Ubuntu with Docker, NIKA, and this host's runtime images, plus the router images the host has. Those are RouterOS and Cisco XRd (see [Prepare vendor images](installation.md#prepare-vendor-images)), and the SONiC virtual switch that `nika images prepare` downloads. The first Firecracker run builds the base, which takes about 20 minutes. Later runs rebuild it when this host's runtime images or Kathara plugins, or NIKA's base image setup, change. Other code changes do not. Lab images receive the current checkout.

The VM cannot build Docker images. A run therefore builds the checkout's `nika/*` images on the host when they are missing or outdated, as a native deploy does, and then rebuilds the base with them. Other NIKA checkouts on the same host share these image tags. When another checkout with different Dockerfiles deploys a lab while the base image is built, the build stops with an error that names the image. Run it again when the other checkout is idle.

The base is assembled in Docker containers, so it needs no `sudo`. The VM has no network of its own. While the base is built, it reaches the network through a proxy on the host.

## What changes in a trial

* A trial's results directory has the same files as a native trial, plus:
  * `image.nika.jsonl`: the image build's events;
  * `run.json` → `firecracker`: restore timings and the lab image used;
  * `env_stop` records that the lab was discarded with its VM copy; `failures_ended` and `session_cleared` come from the VM.
* Restored environments resume transient protocols before failure injection. In `enterprise_branch`, Linux WireGuard edges keep their peers and running configuration while they renegotiate session keys. NIKA briefly suppresses handshake initiations at one end of each tunnel, to prevent simultaneous retries from colliding, and then removes its temporary rules. RouterOS peers keep their restored state. NIKA records unsupported recovery in the environment logs.
* Environments with protocol recovery pass full environment verification. Other environments use the configured [`nika.runtime_validation.depth`](configuration.md#runtime-validation). Set `verify_after_restore: false` to skip verification; protocol recovery still runs. [Case images](#case-images-optional) preserve their injected state, run protocol recovery, then check that the injected failure remains. They skip healthy-environment verification.
* Lab-host events in `nika.jsonl` and `messages.jsonl` carry `"origin": "remote"` and stream to the host about once a second.
* Teardown discards the VM copy instead of undeploying the lab.
* Resume, retries, `--case-timeout`, and `--batch-size` work as in native runs. Copies that a crashed run left behind are deleted when the same results directory runs again, or by `nika firecracker clean`.

### Clock

Before exposing a restored environment, NIKA aligns its UTC clock with the restore host. Guest uptime and monotonic protocol timers continue from the saved state. `run.json` records any clock difference between the agent and lab hosts as `remote_clock_offset_sec`.

* Agent timing metrics use host timestamps only.
* `nika inspect` shifts lab-host rows onto the host clock, so the timeline stays in order. The lab's own time is kept as `lab_timestamp`.
* The log files themselves keep the lab-host time.

## Lab image keys

Lab images are keyed by:

* environment variant;
* code that runs in the VM: `src/` and `pyproject.toml`, without host-only code (agents and their sandboxes, LLM clients, evaluation, `nika inspect`, the TUI, and NIKA's Firecracker host code), so changing an agent keeps the lab images;
* the host's lab deploy settings (`nika.lab`, `runtime_validation`, `static_validation`, `k8s`), which the image deploy uses;
* base image;
* VM size and huge-page setting;
* Firecracker binary, since a lab image restores only with the binary that saved it.

A change to any of these builds a new lab image on the next run, and `list` marks the old one stale. A case image is keyed by its lab image and its task, so a new lab image also builds new case images.

Lab images and restored trials record the native VDE helper image ID, so you can identify the forwarding implementation. Fresh guests install the helper before deployment. Existing snapshots use the hub they saved.

## Performance reference

Firecracker execution makes each trial cheaper in exchange for a one-time build per environment variant, and the disk to keep the result. A lab image build costs about one native deploy and holds 0.3 to 4 GiB. After that, a trial restores its lab in seconds instead of deploying and undeploying it.

For release 0.2.0 (test split, 85 tasks × 3 trials), measured before the release pinned its engine, on a 16 vCPU host that is itself a VM, with huge pages:

| | Native | Lab images | Lab and case images |
|---|---:|---:|---:|
| One-time builds (VM time) | — | 1.5 h | 1.5 h + 2.0 h |
| Disk | — | 12 GiB base + 23 GiB | 12 GiB base + 23 GiB + 3.5 GiB |
| Time outside the agent per run (255 trials) | 10.8 h | 0.9 h | 0.1 h |
| Builds pay for themselves after | — | the first run | about 2.4 more runs |
| Measured wall time, 85 trials 8 at a time | 5 h 55 min | 1 h 55 min, plus 35 min of builds | — |

*Time outside the agent* covers deploy or restore, verification, injection, and teardown. It excludes the agent's own run, which can take longer with many copies at once (see below). A *run* is every task of the split with 3 trials each. [Case images](#case-images-optional) are optional and pay off only when the same tasks run again.

Native runs also take longer in wall time, because heavy labs (Kubernetes, Containerlab, size `l`) run alone, while Firecracker trials run side by side. With 6 to 8 copies at once, tools that probe the network (ping meshes, HTTP tests) took 20 to 150% longer in their median than in the native run, which ran fewer trials at once.

While a lab image is built, its snapshot temporarily needs the VM's whole `vm_memory` of free disk (see [VM size](#vm-size)). Disk sizes are with btrfs, where NIKA stores images zstd-compressed (about 3.5 times smaller). On XFS they are uncompressed. See [Huge pages and disk](#huge-pages-and-disk).

### Compatibility and scores

Restored labs behave like freshly deployed ones:

* All 29 environment variants of release 0.2.0 ran from lab images with the mock agent, with the same scores as native runs.
* Full test-split runs (85 tasks × 3 trials) with Claude Code and with Codex, both on Qwen3-Coder-Next-FP8 and each agent in its sandbox, finished all 255 trials. Both runs are on the [NIKA leaderboard](https://sands-lab.github.io/nika/leaderboard/). Claude Code also ran each task once natively. On these 85 tasks, Firecracker scored higher on 11, lower on 11, and the same on 63.
* RouterOS and Cisco XRd labs restored with their BGP sessions up and scored as in native runs. Their agents started 2.3 s and 3.4 s after the trial began, instead of 70 s and 120 s.

### Per environment

*Per trial* is the median time outside the agent. *Pays off after* is the number of runs of that environment's tasks after which its builds have cost less than the time they saved. Case images count failure tasks only; healthy tasks restore the lab image.

| Environment | Tasks | Native, per trial | Lab image: build, disk | Firecracker, per trial | Pays off after (runs) | Case images: builds, disk | With case images, per trial | Case images pay off after (more runs) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `campus_lan` l | 6 | 196 s | 241 s, 973 MiB | 27 s | 0.08 | 702 s, 417 MiB | 2 s | 1.6 |
| `campus_lan` m | 6 | 103 s | 154 s, 655 MiB | 5 s | 0.09 | 280 s, 134 MiB | 2 s | 5.8 |
| `campus_lan` s | 4 | 87 s | 106 s, 379 MiB | 10 s | 0.12 | 267 s, 81 MiB | 2 s | 2.7 |
| `dc_clos` l | 2 | 296 s | 280 s, 573 MiB | 11 s | 0.16 | 98 s, 60 MiB | 2 s | 3.3 |
| `dc_clos` m | 3 | 67 s | 91 s, 358 MiB | 7 s | 0.17 | 151 s, 52 MiB | 2 s | 3.8 |
| `dc_clos` s | 2 | 36 s | 61 s, 358 MiB | 3 s | 0.31 | 139 s, 31 MiB | 3 s | 0.5 |
| `enterprise_branch` l | 5 | 81 s | 169 s, 399 MiB | 6 s | 0.15 | 388 s, 164 MiB | 2 s | 5.9 |
| `enterprise_branch` m | 4 | 64 s | 89 s, 317 MiB | 11 s | 0.14 | 318 s, 86 MiB | 2 s | 2.3 |
| `enterprise_branch` s | 3 | 43 s | 61 s, 287 MiB | 9 s | 0.20 | 133 s, 54 MiB | 2 s | 2.1 |
| `isp_abilene` s | 1 | 80 s | 152 s, 338 MiB | 6 s | 0.69 | 66 s, 40 MiB | 2 s | 5.3 |
| `isp_dfn-bwin_ebgp_rtbh` s | 1 | 97 s | 113 s, 328 MiB | 6 s | 0.41 | 40 s, 42 MiB | 3 s | 4.5 |
| `isp_di-yuan` s | 1 | 92 s | 145 s, 328 MiB | 18 s | 0.66 | 171 s, 42 MiB | 2 s | 3.4 |
| `isp_geant_ebgp_rpki` m | 2 | 640 s | 661 s, 430 MiB | 7 s | 0.17 | 201 s, 106 MiB | 2 s | 6.4 |
| `isp_germany50` l | 1 | 111 s | 126 s, 512 MiB | 12 s | 0.42 | 141 s, 73 MiB | 2 s | 4.7 |
| `isp_pdh` s | 1 | 237 s | 574 s, 2.8 GiB | 117 s | 1.59 | 449 s, 179 MiB | 5 s | 1.3 |
| `k8s_lab` | 6 | 344 s | 460 s, 4.2 GiB | 23 s | 0.08 | 924 s, 506 MiB | 2 s | 2.8 |
| `llmd_lab` | 5 | 207 s | 258 s, 4.3 GiB | 20 s | 0.09 | 859 s, 457 MiB | 3 s | 3.2 |
| `min3clos` | 1 | 115 s | 207 s, 1.4 GiB | 10 s | 0.66 | 151 s, 64 MiB | 3 s | 7.2 |
| `p4_dc_fabric` l | 1 | 187 s | 192 s, 389 MiB | 9 s | 0.36 | 56 s, 34 MiB | 2 s | 2.8 |
| `p4_dc_fabric` m | 1 | 106 s | 123 s, 328 MiB | 12 s | 0.44 | 48 s, 23 MiB | 5 s | 2.2 |
| `p4_dc_gateway` l | 5 | 173 s | 193 s, 471 MiB | 17 s | 0.08 | 350 s, 185 MiB | 3 s | 1.7 |
| `p4_dc_gateway` m | 7 | 88 s | 118 s, 379 MiB | 8 s | 0.07 | 281 s, 143 MiB | 2 s | 2.7 |
| `p4_dc_gateway` s | 7 | 70 s | 89 s, 338 MiB | 11 s | 0.07 | 391 s, 139 MiB | 3 s | 2.2 |
| `sdn_l3_clos` l | 4 | 298 s | 283 s, 860 MiB | 8 s | 0.08 | 237 s, 262 MiB | 2 s | 4.0 |
| `sdn_l3_clos` m | 2 | 130 s | 168 s, 840 MiB | 4 s | 0.22 | 50 s, 38 MiB | 2 s | 6.4 |
| `sdn_l3_clos` s | 4 | 82 s | 114 s, 901 MiB | 10 s | 0.13 | 248 s, 123 MiB | 2 s | 2.4 |

Lab images pay off within the first run for every environment except `isp_pdh` in the table, which used 4 vCPUs. Those SR Linux environments now get 12 (see [VM size](#vm-size)). Case images pay off after 0.5 to 7 more runs, depending on how long injection takes.
