# p4_int — In-band Network Telemetry

A fixed-size BMv2 leaf-spine fabric with two hosts and an INT collector. The
P4 pipeline adds per-hop telemetry to data traffic and sends reports to the
collector, which stores flow and switch measurements in InfluxDB.

## Topology

```text
                  spine1
                 /      \
pc1 ── leaf1                  leaf2 ── pc2
                 \      /       |
                  spine2     collector
```

| Device | Address | Role |
|--------|---------|------|
| `pc1` | `10.0.0.1/24` | Traffic source |
| `pc2` | `10.0.0.2/24` | Traffic destination |
| `collector` | `10.0.0.3/24` | INT report receiver and InfluxDB |
| `leaf1`, `leaf2`, `spine1`, `spine2` | P4 data plane | INT source/transit/sink functions |

The collector records measurements including `flow_stat`, `flow_hop_latency`,
`port_tx_utilization`, and `sw_queue_occupancy`.

## Build the collector image

The scenario requires its local InfluxDB image:

```shell
docker build -t kathara/influxdb src/nika/net_env/kathara/p4/p4_int
```

The image initializes organization `int_org`, bucket `int_bucket`, and token
`int_token`.

## Deploy

```shell
nika env run p4_int
```

## Verification

```shell
nika exec leaf1 pgrep -a simple_switch
nika exec collector pgrep -a python3
nika exec collector curl -fsS http://localhost:8086/health
nika exec --timeout 30 pc1 ping -c 3 10.0.0.2
```

The deployment verifier checks all seven nodes, the four BMv2 processes, the
collector process and address, and end-to-end host reachability.
