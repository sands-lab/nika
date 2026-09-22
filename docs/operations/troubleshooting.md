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
- Keep `--batch-size 1` for runs that include `k8s_lab` or `llmd_lab`.
- A single quiet `nika env run k8s_lab` can pass with `max_user_instances=128` if `max_user_watches` is already large. Repeated k3s starts (for example full 0.2.0-style matrices) hit the limit sooner.
- After an OOM or hard reset, run the `sysctl` check again before the next XR or k3s lab.

## Still stuck?

If the steps above do not clear the failure, ask on the [NIKA Slack community](https://sands-lab.github.io/nika/community/) or [open a GitHub issue](https://github.com/sands-lab/nika/issues/new) with the error text, `nika` / OS versions, how you launched the run (`nika env run …` or `nika benchmark run …`, including `--batch-size` when relevant), and the relevant logs from your result dir (`nika.jsonl`, `run.json`, `messages.jsonl`, worker logs under `trials/`). For XRd, attach `docker logs` from an exited router container.
