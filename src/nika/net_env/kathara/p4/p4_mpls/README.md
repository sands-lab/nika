# p4_mpls — BMv2 MPLS Fabric

A fixed-size P4 network with seven BMv2 switches and three hosts. Border
switches classify IPv4 destinations into labels, transit switches forward on
the MPLS label, and the egress border removes the MPLS header.

## Topology

```text
                    switch_2
                  /          \
pc1 ─ switch_1                 switch_4 ─ switch_5 ─┐
                  \          /                       ├─ switch_7 ─ pc2
                    switch_3     switch_6 ──────────┘             └ pc3
```

| Host | Address |
|------|---------|
| `pc1` | `10.1.1.2/24` |
| `pc2` | `10.7.2.2/24` |
| `pc3` | `10.7.3.2/24` |

The command files define FEC-to-label mappings and label-switched paths.
Labels `2` and `3` carry traffic from `pc1` toward `pc2` and `pc3`; label `1`
carries return traffic toward `pc1`.

## Deploy

```shell
nika env run p4_mpls
```

## Verification

```shell
nika exec switch_1 pgrep -a simple_switch
nika exec switch_7 pgrep -a simple_switch
nika exec --timeout 30 pc1 ping -c 3 10.7.2.2
nika exec --timeout 30 pc1 ping -c 3 10.7.3.2
```

The deployment verifier checks all ten nodes, every BMv2 process, the three
host addresses, and reachability from `pc1` to both remote hosts.
