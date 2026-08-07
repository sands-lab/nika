# NIKA Remote

Run the agent and CLI locally while a remote host runs the network labs and MCP gateway.

## Roles

| Side | Install | Responsibility |
|------|---------|----------------|
| **Local (agent host)** | `uv sync` (+ sdk/sade as needed), Docker Sandboxes/`sbx` for non-BYO agents | CLI entrypoint, agent process, canonical `results/` |
| **Remote (lab host)** | `uv sync --extra labs` (or `kathara` / `containerlab`), Docker, Kathara/clab | `nika remote serve`, labs, MCP gateway, runtime state |

## Remote host setup

```shell
git clone https://github.com/sands-lab/nika
cd nika
uv sync --extra labs
source .venv/bin/activate

nika remote serve --host 0.0.0.0 --port 8700
```

Ensure the local machine can reach:

1. The daemon port (default `8700`)
2. Ephemeral MCP gateway ports opened by the daemon for each agent run (firewall must allow them from the agent host)

## Local host setup

In `config/nika.yaml`:

```yaml
nika:
  remote:
    enabled: true
    url: http://<lab-host>:8700
```

Leave `enabled: false` for local labs. Remote uses no shared token (trust the network boundary).

Check connectivity:

```shell
nika remote health
# or: nika remote health --url http://<lab-host>:8700
```

## Run a workflow

With remote mode enabled, use the usual workflow commands:

```shell
nika env run simple_bgp
nika failure inject link_down --set host_name=pc1 --set intf_name=eth0
nika session containers 
nika agent run -a mock
nika session close -y
nika eval metrics
```

Artifacts are copied to the local `{nika.result_dir}/{session_id}/` directory (default `results/{session_id}/`), and evaluation runs locally. Remote mode also applies to `nika benchmark run`.

## Smoke checklist

1. On the lab host: `nika remote serve`
2. On the agent host: configure `nika.remote` in `config/nika.yaml`, then run `nika remote health`
3. Run a mock pipeline: env → inject → `nika agent run -a mock` → close → `nika eval metrics`
4. Confirm local `results/<session_id>/` contains synced files and that the remote lab was undeployed after `session close`

## Notes

- Do **not** enable `nika.remote.enabled` in the lab host's `config/nika.yaml`; the daemon always handles labs locally.
