# rip_small_internet_vpn — RIP and WireGuard

A scalable Kathara mini-Internet with a full mesh of internal FRR routers,
an Internet gateway, external service zones, Apache servers, and a WireGuard
overlay.

```text
 internal PCs ─ internal RIP mesh ─ gateway ─ external RIP routers
                                                    |
                                      web servers + VPN server
```

## Scale

| Size | Internal routers | Internal PCs | External routers | Web servers |
|------|-----------------:|-------------:|-----------------:|------------:|
| `s` | 2 | 2 | 1 | 2 |
| `m` | 4 | 4 | 2 | 8 |
| `l` | 8 | 8 | 4 | 32 |

Internal routers form a full mesh. The first two connect to `gateway_router`,
which connects to every `external_router_<n>`.

## Addressing and services

| Network | Use |
|---------|-----|
| `192.168.0.0/16` split into `/31`s | Inter-router links |
| `10.0.<index>.0/24` | Internal PC LANs |
| `20.0.<zone>.0/24` | External server zones |
| `172.16.1.0/24` | WireGuard overlay |

`pc1` is a WireGuard client, `vpn_server_1` is the endpoint at
`20.0.0.2:51820`, and `web_server_1_1`/`web_server_1_2` participate in the
overlay. RIP advertises the infrastructure and attached LANs.

## Deploy

```shell
nika env run rip_small_internet_vpn -s s
```

## Verification

```shell
nika exec --timeout 30 router1 "vtysh -c 'show ip rip'"
nika exec --timeout 30 pc1 ping -c 3 10.0.1.2
nika exec pc1 wg show wg0
nika exec vpn_server_1 wg show wg0
nika exec --timeout 30 pc1 curl -s http://172.16.1.21/
```

The deployment verifier checks RIP/FRR, internal and external reachability,
the WireGuard interfaces, Apache, and HTTP access over the VPN.
