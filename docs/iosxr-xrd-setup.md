# Cisco IOS-XR (XRd) image setup

`iosxr_simple_bgp` runs Cisco's XRd Control Plane container. Cisco's licensing
means this image cannot be redistributed or auto-built like the
`kathara/nika-*` images, so operators load and tag it by hand before
deploying the scenario.

## 1. Obtain the image

Download the XRd Control Plane container tarball from Cisco (a CCO account
with an XRd Control Plane entitlement is required, e.g. through Cisco
Software Download or a Cisco Modeling Labs license). The file looks like:

```text
xrd-control-plane-container-x86_64-<version>.tgz
```

## 2. Load and tag it

Load the tarball into Docker, then tag it to match the image reference the
lab expects:

```shell
docker load -i xrd-control-plane-container-x86_64-<version>.tgz
docker tag <loaded-repo>:<loaded-tag> ios-xr/xrd-control-plane:26.2.1
```

`ios-xr/xrd-control-plane:26.2.1` is the `IMAGE` constant in
[`iosxr_simple_bgp/lab.py`](../src/nika/net_env/kathara/interdomain_routing/iosxr_simple_bgp/lab.py).
If you load a different XRd version, either tag it as `26.2.1` or update that
constant to match.

## 3. Verify the tag

```shell
docker images | grep xrd-control-plane
```

If the tag is missing, `nika env run iosxr_simple_bgp` fails fast with a
`RuntimeError` that repeats the `docker load` / `docker tag` steps above
instead of deploying a broken lab.

## 4. Deploy

```shell
uv run nika env run iosxr_simple_bgp
```

Each router runs privileged and with IPv6 enabled (set via Kathara device
metadata in `lab.py`) — this is standard XRd Control Plane requirements, no
extra host configuration beyond a working Kathara/Docker install.

XRd's ZTP process can briefly race the container's network namespace setup
right after boot; the router startup scripts already retry config
application until that clears, so a slow first boot is expected and not a
failure.
