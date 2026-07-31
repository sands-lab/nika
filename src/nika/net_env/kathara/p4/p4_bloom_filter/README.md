# p4_bloom_filter — P4 Flow Threshold

A fixed-size BMv2 lab with two hosts and two P4 switches. The switches run
`bloom_filter.p4`, which hashes TCP five-tuples into two register positions,
counts matching packets, and drops a flow after both counters exceed the
configured threshold.

## Topology

```text
pc1 (10.0.0.1) ─ switch_1 ─ switch_2 ─ pc2 (10.0.0.2)
```

Both hosts use static ARP entries. Each switch compiles the P4 program during
startup, starts `simple_switch`, and loads its IPv4 forwarding entries from
`cmds/switch_<n>.txt`.

| P4 constant | Value |
|-------------|------:|
| `BLOOM_FILTER_ENTRIES` | 4096 |
| `BLOOM_FILTER_BIT_WIDTH` | 32 |
| `PACKET_THRESHOLD` | 1000 |

## Deploy

```shell
nika env run p4_bloom_filter
```

This scenario has no topology-size option.

## Verification

```shell
nika exec switch_1 pgrep -a simple_switch
nika exec switch_1 "echo show_tables | simple_switch_CLI"
nika exec --timeout 30 pc1 ping -c 3 10.0.0.2
```

The deployment verifier checks all four nodes, both BMv2 processes, host
addresses, and reachability from `pc1` to `pc2`.
