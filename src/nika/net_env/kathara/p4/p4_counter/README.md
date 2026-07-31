# p4_counter — L2 Forwarding Counters

A fixed-size BMv2 fabric with three hosts and four P4 switches. The
`l2_basic_forwarding_counter.p4` program forwards on destination MAC address
and records per-port ingress and egress packet counters.

## Topology

```text
                 s2
               /    \
pc1 ── s1                    s4 ── pc2
               \    /          └── pc3
                 s3
```

All hosts share `10.0.0.0/24`:

| Host | Address |
|------|---------|
| `pc1` | `10.0.0.1` |
| `pc2` | `10.0.0.2` |
| `pc3` | `10.0.0.3` |

Each switch compiles the P4 source, starts `simple_switch`, and loads the
forwarding rules in `cmds/<switch>.txt`.

## Deploy

```shell
nika env run p4_counter
```

## Verification

Generate traffic across both paths and confirm the switches are running:

```shell
nika exec s1 pgrep -a simple_switch
nika exec s4 pgrep -a simple_switch
nika exec --timeout 30 pc1 ping -c 3 10.0.0.2
nika exec --timeout 30 pc1 ping -c 3 10.0.0.3
```

The deployment verifier checks all seven nodes, every BMv2 process, all host
addresses, and reachability from `pc1` to the other hosts.
