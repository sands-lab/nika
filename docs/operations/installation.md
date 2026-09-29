# Installation

Audience: someone setting up NIKA on a Linux host for the first time.

## Default install

```shell
git clone https://github.com/sands-lab/nika.git
cd nika
./scripts/install.sh
```

You need Linux, Python 3.12+, `curl`, and `sudo`. After a fresh Docker install, open a new shell or run `newgrp docker`.

This installs Docker (if needed), uv, Kathará and Python deps, Containerlab, gnmic, clang, iproute2, plus `.env` and `config/nika.yaml` when they are missing. On non-apt systems, install clang and iproute2 yourself. See `./scripts/install.sh --help` for every flag.

The installer also raises `fs.inotify.max_user_instances` and `fs.inotify.max_user_watches` to at least `64000` and persists them in `/etc/sysctl.d/99-nika-inotify.conf`. `k8s_lab`, `llmd_lab`, and `iosxr_simple_bgp` fail at the kernel default. If the installer cannot change them, follow [Host inotify limits too low](troubleshooting.md#host-inotify-limits-too-low-k3s--xrd).

Check that no other route on the host, such as one from a VPN or the host network, overlaps Docker's default subnets (`172.17.0.0/16` and `172.18.0.0/16`). Overlapping routes break container networking and the Kubernetes API of `k8s_lab` and `llmd_lab`. See [VPN routes overlap the Docker bridge subnet](troubleshooting.md#vpn-routes-overlap-the-docker-bridge-subnet).

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

## Prepare vendor images

Skip this unless you need a lab that depends on a third-party router image (for example [`iosxr_simple_bgp`](network-scenarios.md#ios-xr-simple-bgp-scenario) or [`routeros_simple_bgp`](network-scenarios.md#routeros-simple-bgp-scenario)). Ordinary FRR and Containerlab labs do not need it.

Pass `--with-vendor-images` so the installer builds or loads those Docker images. Without the flag, nothing vendor-related is downloaded. Today's supported images:

| Scenario | Docker image | What you provide |
| --- | --- | --- |
| RouterOS | `vrnetlab/mikrotik_routeros:7.21.5` | Nothing. The script downloads CHR from MikroTik and builds the image. Needs `/dev/kvm`. |
| IOS-XR (XRd) | `ios-xr/xrd-control-plane:26.2.1` | A local Cisco `.tgz` (CCO / Modeling Labs). There is no public download URL. |

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

## Related

- [Remote lab execution](remote.md): lab host vs agent host
- [Agent sandboxing](agent-sandbox.md): `sbx` for sandboxed agents
- [Run configuration](configuration.md): `.env` and `config/nika.yaml`
