# Installation

Audience: someone setting up NIKA on a Linux host for the first time.

## Default install

```shell
git clone https://github.com/sands-lab/nika.git
cd nika
./scripts/install.sh
```

You need Linux, Python 3.12+, `curl`, `git`, `sudo`, and a usable Docker Engine. The installer does not install Docker; follow [Install Docker Engine](https://docs.docker.com/engine/install/) first. If your user cannot reach Docker yet, the installer adds it to the `docker` group and continues under that group.

The installer does not use apt or other system package managers. It keeps what it installs inside the repository:

| What | Where |
| --- | --- |
| Kathará and the other Python deps | `.venv/` (or `UV_PROJECT_ENVIRONMENT`) |
| `clab` (Containerlab) and `gnmic` | `.venv/bin/`, pinned release binaries from GitHub, checked against the release checksums |
| uv, when it is not already on `PATH` | `.nika_cache/bin/uv`, linked as `.venv/bin/uv` |
| uv's package cache, and a uv-managed Python when the host has no Python 3.12+ | `.nika_cache/uv/`, unless you set `UV_CACHE_DIR` or `UV_PYTHON_INSTALL_DIR` |
| Runtime image caches and vendor downloads | `.nika_cache/` |
| `.env` and `config/nika.yaml`, when missing | the repository root |

`uv run` puts `.venv/bin` first on `PATH`, so NIKA finds `clab` and `gnmic` there. To use them, or a uv the installer added, in your own shell, run `source .venv/bin/activate`. The installer sets the uv locations only while it runs; uv cannot read a repository-relative cache path from the project config. Later `uv run` and `uv sync` calls use `~/.cache/uv` unless you export `UV_CACHE_DIR`, so the installer ends by printing the `export UV_CACHE_DIR=...` line to add to your shell profile.

Some parts still live outside the repository because they belong to the host:

- Docker images and build cache, in Docker's data root (usually `/var/lib/docker`).
- The `clab` binary in `.venv/bin` is owned by root and setuid, as in Containerlab's own installer. Members of the `clab_admins` group run it as root. The installer uses `sudo` once to set this up and to create the group and add you to it. Open a new login shell afterwards. `.venv` must not be on a `nosuid` mount.
- The inotify and `vrf` kernel settings described below, under `/etc`.

The tools NIKA runs for fault injection and Kubernetes image preparation come in Docker images that `nika images prepare` builds: `nika/tc-bpf` compiles and attaches eBPF programs, and `nika/skopeo` fetches workload images. Containerlab link faults use the host's `tc`, `ip`, and `nsenter`, which most distributions ship by default; the installer warns if one is missing.

The installer also raises `fs.inotify.max_user_instances` and `fs.inotify.max_user_watches` to at least `64000` and persists them in `/etc/sysctl.d/99-nika-inotify.conf`. `k8s_lab`, `llmd_lab`, and `iosxr_simple_bgp` fail at the kernel default. If the installer cannot change them, follow [Host inotify limits too low](troubleshooting.md#host-inotify-limits-too-low-k3s--xrd).

`enterprise_branch` creates Linux VRF devices, which need the host `vrf` kernel module. The installer loads it and persists it in `/etc/modules-load.d/nika-vrf.conf`. Ubuntu cloud images with the `-virtual` kernel ship the module only in `linux-modules-extra-$(uname -r)`; the installer does not install that package and prints the command instead. See [Host kernel lacks the vrf module](troubleshooting.md#host-kernel-lacks-the-vrf-module-enterprise_branch).

Check that no other route on the host, such as one from a VPN or the host network, overlaps a subnet Docker uses (by default `172.17.0.0/16`–`172.31.0.0/16` and `192.168.0.0/16`). Overlapping routes break container networking and the Kubernetes API of `k8s_lab` and `llmd_lab`. See [Host routes overlap Docker subnets](troubleshooting.md#host-routes-overlap-docker-subnets).

## Runtime images

The installer ends by running `uv run nika images prepare`. It builds every local `nika/*` image, pulls the upstream images that non-vendor scenarios deploy (`kathara/*`, `rancher/k3s`, Nokia SR Linux, and others), and caches the `k8s_lab` and `llmd_lab` workload archives and Helm charts under `.nika_cache/`. Benchmarks then start without image builds or registry pulls. Vendor router images still need `--with-vendor-images` (see [Prepare vendor images](#prepare-vendor-images)).

On a fresh 8-vCPU host, the whole first install takes about 20 minutes and uses about 16 GB of disk: 11 GB of Docker images, 4 GB of Docker build cache, and 1 GB under `.nika_cache/`. Pass `--skip-images` to skip this step; labs then build or pull what they need on first deploy.

List the images the step ensures:

```shell
uv run nika images list
```

## Upgrade an existing install

Re-run `./scripts/install.sh` after `git pull` or on a host with an older or partial install. Each run brings the host to what the current checkout needs and prints one line per change:

- Rebuilds a `nika/*` image when its Dockerfile, the files the Dockerfile copies, or a parent image changed. Builds carry an `io.nika.build-hash` label that records these inputs. Images built by older installers have no label, so they rebuild once.
- Removes image tags that the current release no longer references, from repositories only NIKA uses (`nika/*`, legacy `kathara/nika-*`, `rancher/k3s`, MetalLB, llm-d, agentgateway, Routinator, Batfish, SR Linux, and the vendor router images). It also removes the Docker copies of Kubernetes workload images that older releases pulled; those images now live only in `.nika_cache/k8s-images/`. Images used by any container are kept. Generic images such as `postgres` or `debian` are never pruned.
- Removes stale `.nika_cache/` entries: workload archives the labs no longer use, other Helm versions and charts, and RouterOS CHR downloads for other versions. User-supplied XRd tarballs and SNDlib traffic are kept.
- Reinstalls gnmic into `.venv/bin` when the `gnmic` on `PATH` differs from the pinned version, and installs Containerlab there when the `clab` on `PATH` is older than the minimum version. A newer Containerlab is kept, including one installed from a package by an older installer.
- Rewrites an inotify sysctl file from an older installer into the current format.

The installer never rewrites `.env` or `config/nika.yaml`. When `config/nika.yaml` fails validation (for example, it uses a key that this version removed), the installer prints the validation error. Compare the file with `config/nika.example.yaml` and fix it.

A second re-run prints `No stale NIKA images or caches` and builds nothing.

## Choose `main` or `dev`

Most users can skip this section. If you already cloned a branch, leave it alone: the installer does not change git unless you pass `--track`.

| Goal | Command | Git branch |
| --- | --- | --- |
| Run published benchmarks | `./scripts/install.sh --track stable` | `main` |
| Try newest scenarios and unfinished features | `./scripts/install.sh --track latest` | `dev` |

`--track` fails if you have uncommitted changes. Commit or stash first.

After `--track stable`, run cases with a frozen release, for example:

```shell
uv run nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
```

## Install sbx for sandboxed agents

Skip this unless you run sandboxed agents (`cli.*`, `sdk.*`, `community.sade`). Host agents such as `byo.langgraph` do not need it.

```shell
./scripts/install.sh --with-sbx
sbx login
```

The flag installs `docker-sbx` from Docker's apt repo, or runs `get.docker.com` with `SBX=1` when that package is unavailable. sbx ships only as a package, so this opt-in step is the only one that uses apt. It skips the install when `sbx` is already on `PATH`.

sbx runs each sandbox as a KVM microVM, so your user needs read/write on `/dev/kvm`. When it lacks access, the installer adds you to the group that owns the device (usually `kvm`). Open a new login shell (or run `newgrp kvm`), then run `sbx daemon stop` so the next NIKA run starts the daemon with the new group. Without this, `sbx create` fails with `KVM error: Permission denied`. If `/dev/kvm` is missing, enable hardware virtualization (or nested virtualization on a VM).

See [Agent sandboxing](agent-sandbox.md) for credentials and runtime settings.

## Prepare vendor images

Skip this unless you need a lab that depends on a third-party router image (for example [`iosxr_simple_bgp`](network-scenarios.md#ios-xr-simple-bgp-scenario) or [`routeros_simple_bgp`](network-scenarios.md#routeros-simple-bgp-scenario)). Ordinary FRR and Containerlab labs do not need it.

Pass `--with-vendor-images` so the installer builds or loads those Docker images. Without the flag, nothing vendor-related is downloaded. Today's supported images:

| Scenario | Docker image | What you provide |
| --- | --- | --- |
| RouterOS | `vrnetlab/mikrotik_routeros:7.21.5` | Nothing. The script downloads CHR from MikroTik and builds the image. Needs `/dev/kvm`. |
| IOS-XR (XRd) | `ios-xr/xrd-control-plane:26.2.1` | A local Cisco `.tgz` (CCO / Modeling Labs). There is no public download URL. The installer accepts the CCO bundle as downloaded and loads the `*.dockerv1.tgz` image inside it. |

```shell
./scripts/install.sh --with-vendor-images
```

That prepares both images when possible: RouterOS always (CHR download + build), XRd when a local Cisco tarball is available. If the XRd image and tarball are both missing, an interactive install prompts you to download the Cisco `.tgz` (CCO / Modeling Labs) and place it under `.nika_cache/vendor/` before continuing; non-interactive installs warn and continue without XRd.

Vendor artifacts land under `.nika_cache/vendor/` (gitignored): XRd tarballs as `xrd-*.tgz`, CHR zip downloads, and the `hellt/vrnetlab` clone at `vrnetlab/`. Provide XRd via `--xrd-tarball`, `NIKA_XRD_TARBALL`, or a file in that directory. Override the cache root with `NIKA_VENDOR_CACHE`. If the target Docker image tag already exists, the step is skipped.

Manual fallback for each lab: [IOS-XR](network-scenarios.md#ios-xr-simple-bgp-scenario), [RouterOS](network-scenarios.md#routeros-simple-bgp-scenario).

## Allow Containerlab link faults

Link faults on Containerlab labs (packet loss, corruption, rate limits, link down, and link flaps) change the host side of each lab link. NIKA runs `tc`, `ip`, `nsenter`, and `sh` on the host through `sudo -n`, so your user needs passwordless `sudo` for those commands. Kathará labs do not need this.

Check whether `sudo` asks for a password:

```shell
sudo -n "$(command -v tc)" qdisc show dev lo
```

If the command prints the loopback qdisc, you are done. If it prints `sudo: a password is required`, add a sudoers rule. `sh` runs the link-flap worker, so this rule gives your user root on the host; use a dedicated lab host or account if that matters to you.

```shell
echo "$USER ALL=(root) NOPASSWD: $(command -v tc), $(command -v ip), $(command -v nsenter), $(command -v sh)" \
  | sudo tee /etc/sudoers.d/nika-host-tc
sudo chmod 0440 /etc/sudoers.d/nika-host-tc
sudo -n "$(command -v tc)" qdisc show dev lo
```

The last command now prints the loopback qdisc without a password prompt.

## Uninstall

`./scripts/uninstall.sh` removes what the installer and NIKA runs created on the host. It asks for confirmation; pass `-y` to skip the prompt.

```shell
./scripts/uninstall.sh
```

| Removed | Kept |
| --- | --- |
| Running sessions, Kathará and Containerlab labs, the Batfish container | Docker and your docker group membership; sbx and your kvm group membership |
| Docker images NIKA built or pulled, including vendor router images, build parents such as `debian:bookworm-slim`, and legacy tags | A uv installed outside the repository |
| The Kathará Docker network plugin and `~/.config/kathara.conf` | apt packages that older installers added: clang, skopeo |
| `/etc/sysctl.d/99-nika-inotify.conf`, with the original inotify limits restored | The source tree |
| `/etc/sudoers.d/nika-host-tc` | `.env` and `config/` |
| The `clab_admins` group, plus the Containerlab package, `/etc/containerlab`, and `/usr/local/bin/gnmic` that older installers added | `results/` |
| `.venv` (including `clab` and `gnmic`), `.nika_cache` (including uv and its cache, XRd tarballs, and CHR downloads), and `runtime/` | |

An image that a non-NIKA container still uses is kept, and the script prints its name. Pass `--prune-build-cache` to also run `docker builder prune -af`. The Docker build cache is shared with every build on the host, so only use this flag on a dedicated lab host.

If the inotify limits were raised by an installer older than this one, the original values were not recorded. The kernel defaults return after the next reboot.

To install again, run `./scripts/install.sh`.

## Related

- [Remote lab execution](remote.md): lab host vs agent host
- [Agent sandboxing](agent-sandbox.md): `sbx` for sandboxed agents
- [Run configuration](configuration.md): `.env` and `config/nika.yaml`
