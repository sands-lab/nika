# MikroTik RouterOS (vrnetlab) image setup

`routeros_simple_bgp` runs a MikroTik RouterOS Cloud Hosted Router (CHR)
image wrapped by `vrnetlab`. MikroTik's licensing means this image cannot be
redistributed or auto-built like the `kathara/nika-*` images, so operators
download and build it by hand before deploying the scenario.

Unlike `iosxr_simple_bgp`'s XRd Control Plane, which runs as a native
container process, RouterOS via vrnetlab boots a full QEMU VM inside the
container. That changes both the build story and the host prerequisites.

## 1. Obtain the image

Download a Cloud Hosted Router (CHR) image from
[mikrotik.com/download](https://mikrotik.com/download): the `.vmdk` variant
for x86, or `.vdi` for arm64.

## 2. Build the image

Clone upstream `vrnetlab` and build the RouterOS image from its
`mikrotik/routeros` directory:

```shell
git clone https://github.com/hellt/vrnetlab
cd vrnetlab/mikrotik/routeros
# copy the downloaded CHR .vmdk/.vdi into this directory
make docker-image
```

No Dockerfile changes are needed for management access: `sshpass` and
`ssh` already ship with vrnetlab's own base image
(`ghcr.io/srl-labs/vrnetlab-base:0.3.0`). NIKA execs `sshpass`+`ssh` into
the container to reach RouterOS's internal management API (see
[`routeros_api.py`](../src/nika/service/lab/routeros_api.py)) with no
extra packages to install.

Do not change the image's `ENTRYPOINT`/`--connection-mode`: the scenario
passes `--connection-mode macvtap` as a Kathara machine argument at deploy
time (see `lab.py`), because Kathara attaches interfaces before the
container starts, and vrnetlab's default `vrxcon`/`tc` datapaths expect a
data interface to appear only after boot.

Then tag the built image to match the `IMAGE` constant in
[`routeros_simple_bgp/lab.py`](../src/nika/net_env/kathara/interdomain_routing/routeros_simple_bgp/lab.py):

```shell
docker tag <built-tag> vrnetlab/mikrotik_routeros:7.21.5
```

If you build a different RouterOS version, either tag it as `7.21.5` or
update that constant to match.

## 3. Verify the tag

```shell
docker images | grep routeros
```

If the tag is missing, `nika env run routeros_simple_bgp` fails fast with a
`RuntimeError` that repeats the build/tag steps above instead of deploying a
broken lab.

## 4. Configure the host

RouterOS/vrnetlab boots a full QEMU VM per router, unlike XRd's native
container process. The host needs KVM / nested virtualization available:

- `/dev/kvm` must exist and be usable.
- If the host itself is a VM, nested virtualization must be enabled at the
  hypervisor level.

Without KVM, boot is dramatically slower or may not complete at all. No
`inotify` limit tuning is needed here — that requirement was specific to
XRd.

## 5. Deploy

```shell
nika env run routeros_simple_bgp
```

Boot is slower than the FRR and XRd scenarios because each router boots a
nested VM; the scenario's verification window accounts for this, so a slow
first boot is expected and not a failure.
