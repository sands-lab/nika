# simple_bgp — Two-AS BGP

A fixed-size Kathara lab with two FRR routers and one host behind each router.
The routers establish an eBGP session and advertise their host-facing networks.

## Topology

```text
pc1 ── router1 ══ eBGP ══ router2 ── pc2
```

| Device | Address | Role |
|--------|---------|------|
| `pc1` | `195.11.14.2/24` | Host behind `router1` |
| `router1` | `195.11.14.1/24` on the host LAN | FRR BGP router |
| `router2` | `200.1.1.1/24` on the host LAN | FRR BGP router |
| `pc2` | `200.1.1.2/24` | Host behind `router2` |

Links `B` and `C` are the host LANs; link `A` connects the two routers.
The startup files configure host routes and start FRR from the router-specific
`/etc/frr/frr.conf` files.

## Deploy

```shell
nika env run simple_bgp
```

This scenario has no topology-size option.

## Verification

Check the BGP session and end-to-end reachability:

```shell
nika exec --timeout 30 router1 "vtysh -c 'show bgp summary'"
nika exec --timeout 30 router1 "vtysh -c 'show ip route'"
nika exec pc1 ip route
nika exec --timeout 30 pc1 ping -c 3 200.1.1.2
```

`verify_lab()` also checks both routers, FRR, the BGP session, `pc1`'s default
route, and connectivity from `pc1` to `pc2`.
