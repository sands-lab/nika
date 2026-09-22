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
# MikroTik only
./scripts/install.sh --with-vendor-images --skip-xrd

# Cisco XRd only (path to your downloaded tarball)
./scripts/install.sh --with-vendor-images --skip-routeros \
  --xrd-tarball /path/to/xrd-control-plane-container-x86_64-<version>.tgz

# Both
./scripts/install.sh --with-vendor-images \
  --xrd-tarball /path/to/xrd-control-plane-container-x86_64-<version>.tgz
```

You can also set `NIKA_XRD_TARBALL` or drop the file at `vendor/xrd-*.tgz` (gitignored). Downloads cache under `~/.cache/nika/vendor/`. If the target image tag already exists, the step is skipped.

For XRd, the installer also tries to raise inotify limits. Manual fallback for each lab: [IOS-XR](network-scenarios.md#ios-xr-simple-bgp-scenario), [RouterOS](network-scenarios.md#routeros-simple-bgp-scenario).

## Related

- [Remote lab execution](remote.md): lab host vs agent host
- [Agent sandboxing](agent-sandbox.md): `sbx` for sandboxed agents
- [Run configuration](configuration.md): `.env` and `config/nika.yaml`
