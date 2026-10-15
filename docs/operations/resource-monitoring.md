# Monitor lab resource usage

To size a lab host or estimate what a benchmark costs to run, you need to know how much CPU and memory each environment uses, and for how long. NIKA can record this in Prometheus. Every series carries the session, scenario, backend, and node that produced it, so you can compare environments, find the heaviest node, or look up a past run by its session id.

Monitoring is opt-in and works for Kathara and Containerlab alike. It runs on the lab host, binds only to `127.0.0.1`, and is not exposed to agents through MCP tools or telemetry.

## Start monitoring

Prerequisites: Docker with the Compose plugin, on the host that runs the labs.

```shell
uv run nika metrics up      # cAdvisor + Prometheus at http://127.0.0.1:9090
uv run nika metrics serve   # session exporter; keep it running (for example in tmux)
```

Then run sessions or benchmarks as usual. Prometheus records each lab container from deployment until the session closes.

To stop the stack, run `uv run nika metrics down`. Add `--volumes` to also delete the stored history, which is otherwise kept for 30 days.

## Query usage per session

Prometheus keeps these series after a session closes:

| Series | Meaning |
| --- | --- |
| `nika:session_cpu_cores` | CPU cores used by all containers of a session |
| `nika:session_memory_working_set_bytes` | Working-set memory of all containers of a session |
| `nika:container_cpu_cores` | CPU cores used by one node (labels `node`, `name`) |
| `nika:container_memory_working_set_bytes` | Working-set memory of one node |
| `nika:session_containers` | Number of lab containers in a session |

Each series has the labels `session_id`, `lab_name`, `backend`, and `scenario`. Some useful queries:

```promql
# Peak memory of each session over the last day
max_over_time(nika:session_memory_working_set_bytes[1d])

# CPU core-seconds a session consumed over the last day (15 s rule interval)
sum_over_time(nika:session_cpu_cores{session_id="20261015-095605-6e1eaf"}[1d]) * 15

# Average CPU per scenario, to compare environments
avg by (scenario) (avg_over_time(nika:session_cpu_cores[1d]))

# The three nodes using the most CPU right now
topk(3, nika:container_cpu_cores)
```

To read the session's agent and model, join on `nika_session_info`, which the exporter publishes while the session runs:

```promql
nika:session_cpu_cores * on (session_id) group_left (agent_type, model) nika_session_info
```

## How it works

[cAdvisor](https://github.com/google/cadvisor) measures every Docker container on the host, which covers both backends. The `nika metrics serve` exporter (port `9464`) reads the local session store and publishes `nika_container_info`, which maps each Docker container `name` to its session and node. Recording rules in [`rules.yml`](../../src/nika/metrics/stack/rules.yml) join the two and store the result under the session labels, so the attribution persists after the exporter stops listing the session.

The measurements cover lab containers only. They exclude the agent process, model inference, and agent sandboxes. Containers that NIKA does not attribute to a session, such as the monitoring stack itself, remain visible in the raw cAdvisor series (`container_cpu_usage_seconds_total{name="..."}`).

The exporter must run on the lab host with the same checkout as the sessions you want to observe, because it reads that checkout's `runtime/` session store. With [remote lab execution](remote.md), run both commands on the lab host.

The stack uses ports `8098` (cAdvisor), `9090` (Prometheus), and `9464` (exporter), all on `127.0.0.1`. Change them in [`docker-compose.yml`](../../src/nika/metrics/stack/docker-compose.yml) and [`prometheus.yml`](../../src/nika/metrics/stack/prometheus.yml) if they are taken.
