# Enterprise OSPF Scenarios

These Kathara scenarios model a hierarchical enterprise network with three
core routers, distribution routers, bridged access switches, user LANs, and a
central server farm. FRR advertises backbone and service networks with OSPF.

```text
 access hosts ─ access switch ─ distribution ─ core triangle
                                                   |
                                          server access router
                                                   |
                                      DNS and HTTP services
```

Core links and routed uplinks use `/31` networks from `172.16.0.0/16`.
User LANs use `10.<core>.<distribution>.0/24`; the server farm uses
`10.200.0.0/24`.

## Scale

Counts below include both user-facing core branches.

| Size | Distribution routers | Access switches | User hosts |
|------|---------------------:|----------------:|-----------:|
| `s` | 2 | 2 | 2 |
| `m` | 4 | 8 | 16 |
| `l` | 8 | 32 | 128 |

Common device names include `router_core_<n>`,
`switch_access_<core>_<dist>_<access>`, and
`pc_<core>_<dist>_<access>_<host>`.

## ospf_enterprise_static

Hosts have static addresses and default routes. Distribution routers are named
`switch_dist_<core>_<index>`. The server farm contains:

| Device | Role |
|--------|------|
| `dns_server` | Authoritative BIND server for the `local` zone |
| `web_server_0`–`web_server_3` | Apache sites `web0.local`–`web3.local` |
| `switch_server_access` | OSPF attachment for `10.200.0.0/24` |

```shell
nika env run ospf_enterprise_static -s s
nika exec --timeout 30 router_core_1 "vtysh -c 'show ip ospf neighbor'"
nika exec pc_1_1_1_1 ip route
nika exec pc_1_1_1_1 getent hosts web0.local
nika exec --timeout 30 pc_1_1_1_1 curl -s http://web0.local
```

## ospf_enterprise_dhcp

Hosts obtain addresses from `dhcp_server`; distribution routers are named
`router_dist_<core>_<index>` and relay DHCP requests to the server farm. This
variant also adds:

| Device | Role |
|--------|------|
| `load_balancer` | NGINX endpoint published as `web99.local` |
| `backend_web_0`–`backend_web_2` | Backends on private network `20.200.0.0/24` |
| `dhcp_server` | Leases `.10`–`.100` in every user LAN |

The backend network is reachable through the load balancer, not directly from
user hosts.

```shell
nika env run ospf_enterprise_dhcp -s s
nika exec pc_1_1_1_1 ip -4 addr show dev eth0
nika exec router_dist_1_1 pgrep -a dhcrelay
nika exec pc_1_1_1_1 getent hosts web99.local
nika exec --timeout 30 pc_1_1_1_1 curl -s http://web99.local
```

Both variants verify OSPF convergence, cross-branch host reachability, DNS,
and HTTP service access after deployment.
