# Data-Center Clos Scenarios

This package defines two scalable Kathara Clos fabrics. Both use FRR eBGP,
`172.16.0.0/16` split into `/31` inter-router links, and leaf service networks
under `10.<pod>.<leaf>.0/24`.

```text
                 super-spine
                /     |      \
             spine  spine   spine
                \     |      /
                  leaf
                    |
               host/service
```

## dc_clos_bgp

`dc_clos_bgp` attaches one host to every leaf. Each leaf advertises its `/24`
host network; super-spines use AS 65000, spines use AS 651xx, and leaves use
AS 652xx.

| Size | Super-spines / pods | Spines per pod | Leaves per pod | Hosts |
|------|---------------------|----------------|----------------|------:|
| `s` | 1 | 2 | 2 | 2 |
| `m` | 2 | 4 | 4 | 8 |
| `l` | 4 | 8 | 8 | 32 |

Device names follow these patterns:

- `super_spine_router_<pod>`
- `spine_router_<pod>_<index>`
- `leaf_router_<pod>_<index>`
- `pc_<pod>_<leaf>`

Deploy and verify the small fabric:

```shell
nika env run dc_clos_bgp -s s
nika exec --timeout 30 super_spine_router_0 "vtysh -c 'show bgp summary'"
nika exec --timeout 30 leaf_router_0_0 "vtysh -c 'show ip route'"
nika exec --timeout 30 pc_0_0 ping -c 3 10.0.1.2
```

## dc_clos_service

`dc_clos_service` replaces leaf hosts with DNS and HTTP services. Each pod has
one authoritative BIND server for `pod<index>`, HTTP servers named
`webserver<index>_pod<pod>`, and an external `client_<pod>`. Clients resolve
names such as `web0.pod0` and access them through the routed fabric.

| Size | Pods | Spines per pod | Leaves per pod | DNS servers | Web servers | Clients |
|------|-----:|----------------|----------------|------------:|------------:|--------:|
| `s` | 1 | 2 | 2 | 1 | 1 | 1 |
| `m` | 2 | 4 | 4 | 2 | 6 | 2 |
| `l` | 4 | 4 | 8 | 4 | 28 | 4 |

Service subnets use `10.<pod>.<leaf>.0/24`; client networks use
`192.168.<pod>.0/24`. The web servers run Python's HTTP server on port 80.

```shell
nika env run dc_clos_service -s s
nika exec client_0 getent hosts web0.pod0
nika exec --timeout 30 client_0 ping -c 3 10.0.1.2
nika exec --timeout 30 client_0 curl -s http://web0.pod0
```

For either scenario, use `nika session inspect` to see the deployed instance
and `nika session close -y` when finished.
