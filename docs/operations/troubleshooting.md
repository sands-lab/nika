# Troubleshooting

Match the error or symptom below, apply the fix, then re-run the same command. Sections cover host and lab bring-up today; agent and eval failures can land here later.

Related references: [Network scenarios](network-scenarios.md), [CLI reference](cli-reference.md), [NIKA Slack community](https://sands-lab.github.io/nika/community/).

## Host inotify limits too low (k3s / XRd)

<a id="k3s-controller-container-not-running-k8s_lab--llmd_lab"></a>

Raise `fs.inotify.max_user_instances` and `fs.inotify.max_user_watches` on the Docker host before you run `k8s_lab`, `llmd_lab`, or `iosxr_simple_bgp`. Many kernels default `max_user_instances` to `128`. k3s and Cisco XRd need far more, so those containers exit and NIKA fails lab verification.

| Scenario | What consumes inotify | Host requirement |
| --- | --- | --- |
| `k8s_lab`, `llmd_lab` | Six privileged k3s nodes and in-cluster components | One shared pool; concurrent labs compete |
| `iosxr_simple_bgp` | Each Cisco XRd Control Plane router (IOS XR ≥ 7.9.2) | About 4000 instances and 4000 watches per router |

XRd host prerequisites: [XRd host setup tutorial](https://xrdocs.io/virtual-routing/tutorials/2022-08-22-setting-up-host-environment-to-run-xrd).

### Match these symptoms

**`k8s_lab` / `llmd_lab`:** Lab verification aborts while the environment is still coming up (before failure injection):

```text
Lab verification aborted for 'k8s_lab__…': k3s node container(s) not running: ['controller']
```

`nika.jsonl` or the worker log records `env_verify_failed` with the same text. Tracker: [sands-lab/nika#54](https://github.com/sands-lab/nika/issues/54#issuecomment-5743222111).

**`iosxr_simple_bgp`:** Verification fails checks such as `router1_bgp_established` or `pc1_gateway_reachable` (lab `VERIFY_MAX_WAIT_SEC` is 480). `pc*` containers stay up; XR routers exit:

```shell
docker ps -a
docker logs <xr-router-container>
```

Router logs include:

```text
ERROR - Not enough inotify instance resource available for new XRd instance.
Current settings are 128 max_user_instances and …
[ERROR  ] Insufficient inotify resources
[ERROR  ] XRd hit a critical error during initialization and has aborted launch
```

### Cause

`fs.inotify.max_user_instances` or `fs.inotify.max_user_watches` is still at the kernel default, or you raised it with `sysctl -w` only. Values from `sysctl -w` disappear after reboot, kernel reset, or OOM recovery unless you persist them under `/etc/sysctl.d/`.

### If limits stay low

- XR or k3s containers exit while peer or PC containers still look healthy.
- Verify blames BGP, gateway, or the k3s controller; the host limit is the real fault.
- `nika env run`, `nika agent run`, and `nika benchmark run` for these scenarios stop before inject or agent work.
- `--batch-size` above `1` on Kubernetes scenarios starts multiple k3s labs and drains the pool faster.

### Fix

1. Read the current limits:

```shell
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
```

2. Raise them for the running kernel:

```shell
sudo sysctl -w fs.inotify.max_user_instances=64000
sudo sysctl -w fs.inotify.max_user_watches=64000
```

3. Persist across reboot (path varies by distro). On many Debian/Ubuntu hosts:

```shell
echo 'fs.inotify.max_user_instances=64000' | sudo tee /etc/sysctl.d/99-nika-inotify.conf
echo 'fs.inotify.max_user_watches=64000' | sudo tee -a /etc/sysctl.d/99-nika-inotify.conf
sudo sysctl --system
```

4. Wipe leftover labs and retry:

```shell
uv run nika session wipe -y
uv run nika env run iosxr_simple_bgp
```

Use `k8s_lab` or `llmd_lab` instead of `iosxr_simple_bgp` when that is the failing scenario.

### Confirm success

```shell
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
```

Both values should be `64000`. After `nika env run`, lab verification completes: XR routers stay `Up`, or the k3s `controller` container stays running.

### Notes

- You do not need to rebuild Docker images or reload the XRd tarball.
- Keep the default `heavy_batch_size: 1` for runs that include `k8s_lab` or `llmd_lab`.
- A single quiet `nika env run k8s_lab` can pass with `max_user_instances=128` if `max_user_watches` is already large. Repeated k3s starts (for example full 0.2.0-style matrices) hit the limit sooner.
- After an OOM or hard reset, run the `sysctl` check again before the next XR or k3s lab.

## Host kernel lacks the vrf module (enterprise_branch)

Every `enterprise_branch` Site Edge binds its business LANs to Linux VRF devices. Containers cannot load kernel modules, so the Docker host must provide `vrf`.

### Match these symptoms

Lab verification waits and times out before failure injection, with the overlay checks failing on every branch:

```text
Lab verification pending for enterprise_branch__…: … failed=[overlay_hq_corp_on_br1, overlay_hq_server_on_br1, e2e_br1_to_hq_server, …]
```

On a Site Edge, BGP sessions are `Established` with `0` prefixes, FRR shows the VRFs as `inactive`, and `/var/log/startup.log` contains:

```text
++ ip link add vrf_corp type vrf table 10
Error: Unknown device type.
```

### Cause

The running kernel has no `vrf` module. Ubuntu `-virtual` and cloud kernels ship `vrf.ko` only in `linux-modules-extra-<kernel>`. Without VRF devices, FRR cannot redistribute the LAN prefixes, so the overlay carries no routes.

### Fix

```shell
sudo apt-get install linux-modules-extra-$(uname -r)
sudo modprobe vrf
echo vrf | sudo tee /etc/modules-load.d/nika-vrf.conf
uv run nika session wipe -y
```

Re-running `./scripts/install.sh` does the same. After a kernel upgrade, install the matching `linux-modules-extra` package before the next `enterprise_branch` lab.

### Confirm success

```shell
lsmod | grep '^vrf'
uv run nika env run enterprise_branch -s s
```

Lab verification passes with `overlay_hq_*` and `e2e_*_to_hq_server` checks `true`.

## Containerlab deploy OOM on memory-tight hosts

Containerlab starts many Nokia SR Linux nodes and PC endpoints in one lab. Parallel create and wiring can push host RAM over the edge even when the running lab would fit. NIKA passes `clab deploy --max-workers` from `nika.lab.containerlab_max_workers` (default `2`) to limit that peak.

### Match these symptoms

- The Docker host becomes unresponsive or reboots while `nika env run … --backend containerlab` (or a Containerlab benchmark case) is still deploying.
- `dmesg` or `journalctl -k` shows an OOM killer entry for `dockerd`, `containerd`, or a `clab-*` container during deploy.
- Deploy fails or the host recovers after swap thrashing, then a wipe and retry sometimes succeeds on a quieter host.

Typical scenarios: `min3clos`, `isp_*` with `--backend containerlab` (especially topologies near the Containerlab size cap; see [Network scenarios](network-scenarios.md#concurrency-and---batch-size)).

### Cause

`clab deploy` creates nodes and virtual wires concurrently. On hosts around 16 GiB RAM, several SR Linux starts at once can exceed available memory before steady-state RSS settles. Raising `heavy_batch_size` above `1` for Containerlab cases compounds the problem across labs.

### Fix

1. Wipe leftovers:

```shell
uv run nika session wipe -y
```

2. Lower deploy concurrency in `config/nika.yaml` (copy from `config/nika.example.yaml` if needed):

```yaml
nika:
  lab:
    containerlab_max_workers: 1
```

3. Keep the default `heavy_batch_size: 1` for runs that include Containerlab scenarios (see [configuration](configuration.md#benchmark-settings)).

4. Retry the same scenario:

```shell
uv run nika env run isp_dfn-bwin --backend containerlab
```

### Confirm success

Deploy completes and prints a `session_id=…`. During deploy, `ps` / `pgrep -a clab` shows `--max-workers 1` (or whatever you set). Host available memory stays above a small cushion instead of collapsing to near zero.

### Notes

- Lower workers slow deploy; they do not reduce steady-state memory once every node is up.
- If the lab is still too large after `containerlab_max_workers: 1`, use a smaller topology or a host with more RAM. Catalog Containerlab ISP cases already prefer topologies with at most 11 routers.
- Setting details: [`nika.lab.containerlab_max_workers`](configuration.md#lab-lifecycle-settings).

## Containerlab link faults fail with a sudo password prompt

Failure injection on a Containerlab lab stops with an error like this:

```text
host command failed (tc qdisc replace dev veth1a2b root netem loss 30%): sudo: a password is required
```

### Cause

NIKA applies Containerlab link faults to the host side of the lab link. It runs `tc`, `ip`, `nsenter`, and `sh` through `sudo -n`, which fails instead of prompting when your user has no passwordless `sudo` rule for them. NIKA cannot change your sudoers policy.

### Fix

Add the sudoers rule from [Allow Containerlab link faults](installation.md#allow-containerlab-link-faults), then re-run the same command.

### Confirm success

`sudo -n "$(command -v tc)" qdisc show dev lo` prints the loopback qdisc without a prompt, and the fault injects.

## Host routes overlap Docker subnets

By default, Docker gives the `docker0` bridge `172.17.0.0/16` and allocates new Docker networks from `172.17.0.0/16`–`172.31.0.0/16`, then `192.168.0.0/16`. Docker skips ranges that are already routed when it creates a network, but a route added later, for example by a VPN client or a change to the host network, can overlap a subnet Docker already uses. The VPN interface can own a different subnet, for example `172.19.1.0/24`, and still route those ranges into the tunnel. Reply packets for containers then leave through that route instead of the Docker bridge.

### Match these symptoms

**Containers have no internet access** while the host does:

```shell
docker run --rm alpine ping -c 2 1.1.1.1
```

The ping reports 100% packet loss, while `ping -c 2 1.1.1.1` on the host succeeds.

**`k8s_lab` / `llmd_lab`:** the lab deploys, but the host cannot reach the Kubernetes API port that the lab publishes. The k8s MCP tools and the submission step fail with connection resets, so agent runs on these scenarios do not finish.

### Cause

The route table has two routes for the Docker bridge subnet, and the kernel picks the VPN route. Check it:

```shell
ip route show 172.17.0.0/16
ip route get 172.17.0.2
```

This section applies when the output shows a route through the VPN interface, for example:

```text
172.17.0.0/16 via 172.19.1.1 dev tun0
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1
172.17.0.2 via 172.19.1.1 dev tun0 src 172.19.1.13
```

The VPN client owns these routes, so NIKA cannot fix them from inside a lab.

### Fix

Move Docker to address ranges that no other host route covers. The VPN or host network configuration stays unchanged.

1. List the ranges the host already routes, and pick two private ranges that do not appear in the output. The example below uses `10.210.0.0/24` for `docker0` and `10.211.0.0/16` for new Docker networks.

```shell
ip route
```

2. List running NIKA sessions. Close only the sessions you own before restarting Docker. The restart in step 4 stops other Docker containers on this host too, so arrange a maintenance window if they are in use.

```shell
uv run nika session ps
uv run nika session close --session_id <YOUR_SESSION_ID>
```

3. Back up `/etc/docker/daemon.json` if it exists. Open it and add these keys to its top-level JSON object, keeping its other settings:

```shell
if sudo test -f /etc/docker/daemon.json; then
  sudo cp -a /etc/docker/daemon.json /etc/docker/daemon.json.bak
fi
sudoedit /etc/docker/daemon.json
```

Use these values with the unused ranges chosen in step 1:

```json
{
  "bip": "10.210.0.1/24",
  "default-address-pools": [
    {"base": "10.211.0.0/16", "size": 24}
  ]
}
```

4. Restart Docker:

```shell
sudo systemctl restart docker
```

### Confirm success

```shell
ip route get 10.210.0.2
docker run --rm alpine ping -c 2 1.1.1.1
docker network create nika-subnet-check
docker network inspect nika-subnet-check --format '{{json .IPAM.Config}}'
docker network rm nika-subnet-check
```

`ip route get` reports `dev docker0`, the container ping succeeds, and the new network gets a subnet from `10.211.0.0/16`. Re-run the failed `nika env run`, or resume the benchmark with `nika benchmark run … --resume`.

### Notes

- Existing Docker networks keep their old subnets. Networks that NIKA creates for new labs use the new pool.
- Pick ranges that lab topologies do not use for their own addresses. For example, `campus_lan` uses `10.200.0.0/24`.
- On Docker Desktop, set the same keys under **Settings > Docker Engine**.

## Still stuck?

If the steps above do not clear the failure, ask on the [NIKA Slack community](https://sands-lab.github.io/nika/community/) or [open a GitHub issue](https://github.com/sands-lab/nika/issues/new) with the error text, `nika` / OS versions, how you launched the run (`nika env run …` or `nika benchmark run …`, including `--batch-size` when relevant), and the relevant logs from your result dir (`nika.jsonl`, `run.json`, `messages.jsonl`, worker logs under `trials/`). For XRd, attach `docker logs` from an exited router container.
