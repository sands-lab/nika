# Monitor lab resource usage

To size a lab host or estimate what a benchmark costs to run, you need to know how much CPU and memory each environment uses, and for how long. NIKA can record this in Prometheus and save a summary next to each session's results. Every measurement carries the session, scenario, backend, and node that produced it, so you can compare environments, find the heaviest node, or look up a past run by its session id.

Monitoring is off by default and works for Kathara and Containerlab alike. It runs on the lab host, binds only to `127.0.0.1`, and is not exposed to agents through MCP tools or telemetry.

## Turn monitoring on

Prerequisites: Docker with the Compose plugin, on the host that runs the labs.

```shell
uv run nika observability on
```

This starts cAdvisor and Prometheus at <http://127.0.0.1:9090>. They keep running, including across Docker restarts, until you turn monitoring off. While monitoring is on, every environment NIKA deploys is recorded automatically, whether it comes from `nika env run`, `nika benchmark run`, `nika case`, or the interactive shell. No other command or flag is needed.

When a session closes, NIKA writes `resources.json` to the session's results directory:

```json
{
  "session_id": "20261015-142642-55ba69",
  "scenario": "min3clos",
  "backend": "containerlab",
  "duration_sec": 162.2,
  "cpu_seconds": 314.03,
  "cpu_cores_avg": 1.936,
  "cpu_cores_peak": 3.213,
  "memory_peak_bytes": 4426428416,
  "nodes": {
    "leaf1": {"cpu_seconds": 94.44, "memory_peak_bytes": 1473523712},
    "...": {}
  }
}
```

`uv run nika observability status` shows whether monitoring is on. `uv run nika observability off` stops it and keeps the recorded history, 30 days by default, for the next time you turn it on. Add `--purge` to delete the history as well.

## Query usage in Prometheus

Prometheus keeps these series after a session closes:

| Series | Meaning |
| --- | --- |
| `nika:session_cpu_cores` | CPU cores used by all containers of a session |
| `nika:session_memory_working_set_bytes` | Working-set memory of all containers of a session |
| `nika:container_cpu_cores` | CPU cores used by one node (labels `node`, `name`) |
| `nika:container_cpu_seconds_total` | CPU time one node has used since its container started |
| `nika:container_memory_working_set_bytes` | Working-set memory of one node |
| `nika:session_containers` | Number of lab containers in a session |

Each series has the labels `session_id`, `lab_name`, `backend`, and `scenario`. Some useful queries:

```promql
# Peak memory of each session over the last day
max by (session_id, scenario) (max_over_time(nika:session_memory_working_set_bytes[1d]))

# Average CPU per scenario, to compare environments
avg by (scenario) (avg_over_time(nika:session_cpu_cores[1d]))

# The three nodes that used the most CPU in the last hour
topk(3, max by (session_id, node) (max_over_time(nika:container_cpu_cores[1h])))
```

While a session runs, `nika_session_info` also carries its `agent_type` and `model`:

```promql
nika:session_cpu_cores * on (session_id) group_left (agent_type, model) nika_session_info
```

## How it works

[cAdvisor](https://github.com/google/cadvisor) measures every Docker container on the host, which covers both backends. When NIKA deploys an environment while monitoring is on, it also starts a session exporter in the background. The exporter reads the checkout's session store and publishes `nika_container_info`, which maps each Docker container `name` to its session and node. It stops when the checkout's last session closes. Recording rules in [`rules.yml`](../../src/nika/metrics/stack/rules.yml) join the two sources and store the result under the session labels, so the attribution persists after the session ends.

Several checkouts on one host share the same Prometheus. Each checkout runs its own exporter on a free port and registers it in `~/.cache/nika/metrics/targets/`. The exporter logs to `runtime/metrics/exporter.log`.

The measurements cover lab containers only. They exclude the agent process, model inference, and agent sandboxes. Containers that NIKA does not attribute to a session remain visible in the raw cAdvisor series, for example `container_cpu_usage_seconds_total{name="..."}`.

With [remote lab execution](remote.md), run `nika observability on` on the lab host.

The stack uses ports `8098` (cAdvisor) and `9090` (Prometheus) on `127.0.0.1`. Change them in [`docker-compose.yml`](../../src/nika/metrics/stack/docker-compose.yml) and [`prometheus.yml`](../../src/nika/metrics/stack/prometheus.yml) if they are taken.
