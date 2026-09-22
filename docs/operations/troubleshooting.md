# Troubleshooting

Match the error or symptom below, apply the fix, then re-run the same command. Sections cover host and lab bring-up today; agent and eval failures can land here later.

Related references: [Network scenarios](network-scenarios.md), [CLI reference](cli-reference.md).

## k3s controller container not running (`k8s_lab` / `llmd_lab`)

### Symptoms

Environment start fails during lab verification with:

```text
Lab verification aborted for 'k8s_lab__…': k3s node container(s) not running: ['controller']
```

`nika.jsonl` (or the worker log) often records `env_verify_failed` with the same message. The failure happens while bringing the lab up, before failure injection. See [sands-lab/nika#54](https://github.com/sands-lab/nika/issues/54#issuecomment-5743222111).

### Cause

`k8s_lab` and `llmd_lab` run six privileged k3s nodes inside Kathará. k3s and in-cluster components open many Linux inotify watches. On hosts where `fs.inotify.max_user_instances` or `fs.inotify.max_user_watches` stay at low defaults, the k3s server can exit. NIKA then reports the `controller` container as not running.

The same limits also apply to Cisco XRd (`iosxr_simple_bgp`). See [IOS-XR simple BGP](network-scenarios.md#ios-xr-simple-bgp-scenario).

### Fix

1. Check current limits:

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

4. Clear leftover labs, then retry:

```shell
uv run nika session wipe -y
uv run nika env run k8s_lab
```

### Notes

- Raising inotify does not require rebuilding Docker images.
- A single `nika env run k8s_lab` can succeed on a quiet host even with `max_user_instances=128` if `max_user_watches` is already large. Full 0.2.0-style runs that start k8s repeatedly are more likely to hit the limit.
- Prefer `--batch-size 1` for Kubernetes scenarios so multiple k3s labs do not compete for host inotify capacity.

## Still stuck?

If the steps above do not clear the failure, [open a GitHub issue](https://github.com/sands-lab/nika/issues/new) with the error text, `nika` / OS versions, how you launched the run (`nika env run …` or `nika benchmark run …`, including `--batch-size` when relevant), and the relevant logs from your result dir (`nika.jsonl`, `run.json`, `messages.jsonl`, worker logs under `trials/`).
