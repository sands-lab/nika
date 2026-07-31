# SDN Scenarios

The two scalable Kathara SDN scenarios use Open vSwitch data planes and a POX
controller at `20.0.0.100:6633`. Hosts share `10.0.0.0/24`; switches use the
`20.0.0.0/24` control network.

## sdn_star

`switch_0` is the center of the star. Each edge switch connects one host and
the center, while every switch also connects to the controller network.

```text
pc1 ─ switch_1 ─┐
pc2 ─ switch_2 ─┼─ switch_0
pcN ─ switch_N ─┘
```

| Size | Edge switches | Hosts | Total switches |
|------|--------------:|------:|---------------:|
| `s` | 4 | 4 | 5 |
| `m` | 8 | 8 | 9 |
| `l` | 16 | 16 | 17 |

The controller runs POX `forwarding.l2_learning`.

```shell
nika env run sdn_star -s s
nika exec switch_0 ovs-vsctl show
nika exec switch_1 ovs-ofctl show switch_1
nika exec --timeout 30 pc1 ping -c 3 10.0.0.2
```

## sdn_clos

Every leaf connects to every spine; each leaf also connects its own group of
hosts. POX discovery and spanning tree suppress flooding on redundant paths
before `forwarding.l2_learning` handles host traffic.

| Size | Spines | Leaves | Hosts per leaf | Total hosts |
|------|-------:|-------:|---------------:|------------:|
| `s` | 1 | 2 | 2 | 4 |
| `m` | 2 | 4 | 4 | 16 |
| `l` | 4 | 8 | 8 | 64 |

Host names use `pc_<leaf>_<index>` and receive sequential addresses from
`10.0.0.0/24`.

```shell
nika env run sdn_clos -s s
nika exec spine_1 ovs-vsctl show
nika exec leaf_1 ovs-ofctl dump-flows leaf_1
nika exec --timeout 30 pc_1_1 ping -c 3 10.0.0.3
```

Both verifiers check the controller process, OVS readiness, host addressing,
and host-to-host connectivity.
