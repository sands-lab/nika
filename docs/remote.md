# Run labs on a remote host

This guide is for operators who run the NIKA CLI and agent on one machine and the network lab plus MCP gateway on another.

Implementation: [`remote/server.py`](../src/nika/remote/server.py) serves the lab API, and [`remote/workflows.py`](../src/nika/remote/workflows.py) connects local workflows to it.

## Split responsibilities between hosts

| Side | Install | Responsibility |
|------|---------|----------------|
| **Local (agent host)** | `uv sync` (+ sdk/sade as needed), Docker Sandboxes/`sbx` for non-BYO agents | CLI entrypoint, agent process, canonical `results/` |
| **Remote (lab host)** | `uv sync --extra labs` (or `kathara` / `containerlab`), Docker, Kathara/clab | `nika remote serve`, labs, MCP gateway, runtime state |

## Set up the remote lab host

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

## Set up the local agent host

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

Confirm that `results/<session_id>/` on the agent host contains the synced session files after the workflow. `nika session close -y` must also remove the lab from the remote host.

## Operational constraints

- Keep `nika.remote.enabled` disabled in the lab host's `config/nika.yaml`. The daemon handles labs on that host.
