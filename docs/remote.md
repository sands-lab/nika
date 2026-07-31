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

# Optional shared token
export NIKA_REMOTE_TOKEN=change-me

nika remote serve --host 0.0.0.0 --port 8700
```

Ensure the local machine can reach:

1. The daemon port (default `8700`)
2. Ephemeral MCP gateway ports opened by the daemon for each agent run (firewall must allow them from the agent host)

## Local host setup

In `.env` (or the environment):

```shell
NIKA_REMOTE_ENABLED=true
NIKA_REMOTE_URL=http://<lab-host>:8700
NIKA_REMOTE_TOKEN=change-me   # must match the daemon when set
```

Leave `NIKA_REMOTE_ENABLED` unset or `false` for local labs.

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

Artifacts are copied to the local `results/{session_id}/` directory (or `NIKA_RESULT_DIR`), and evaluation runs locally. Remote mode also applies to `nika benchmark run`.

## Smoke checklist

1. On the lab host: `nika remote serve`
2. On the agent host: set `NIKA_REMOTE_*`, then `nika remote health`
3. Run a mock pipeline: env → inject → `nika agent run -a mock` → close → `nika eval metrics`
4. Confirm local `results/<session_id>/` contains synced files and that the remote lab was undeployed after `session close`

## Notes

- Do **not** set `NIKA_REMOTE_ENABLED=true` on the lab host running `nika remote serve`.
