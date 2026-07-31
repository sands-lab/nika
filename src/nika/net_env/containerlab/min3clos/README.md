# min3clos — SR Linux Clos

A fixed five-node Containerlab fabric based on the Containerlab
[`min-clos`](https://containerlab.dev/lab-examples/min-clos/) example. Two
Nokia SR Linux leaves connect through one spine, with one Linux client behind
each leaf.

## Topology

```text
client1 ─ leaf1 ─ spine ─ leaf2 ─ client2
```

| Node | Management IPv4 | Data-plane role |
|------|-----------------|-----------------|
| `leaf1` | `172.100.100.2` | AS 65001; client gateway `10.0.0.24` |
| `leaf2` | `172.100.100.3` | AS 65002; client gateway `10.0.0.26` |
| `spine` | `172.100.100.4` | AS 65056 |
| `client1` | `172.100.100.5` | `10.0.0.25/31` |
| `client2` | `172.100.100.6` | `10.0.0.27/31` |

The SR Linux nodes run eBGP between leaves and spine. Their configuration also
enables OSPFv2 and IS-IS on fabric interfaces. NIKA waits for gNMI, pushes the
YAML configurations with `gnmic`, and configures both Linux clients.

## Prerequisites

- Docker and Containerlab (`clab`)
- `gnmic` on `PATH`
- `ghcr.io/nokia/srlinux:24.10`
- `ghcr.io/hellt/network-multitool`

Install the backend dependencies with:

```shell
uv sync --extra containerlab
```

## Deploy

```shell
nika env run min3clos
```

This scenario has a fixed size; omit `-s`.

## Verification

```shell
nika exec --timeout 30 leaf1 "sr_cli 'show network-instance default protocols bgp neighbor'"
nika exec client1 ip addr show dev eth1
nika exec --timeout 30 client1 ping -c 3 10.0.0.24
nika exec --timeout 30 client1 ping -c 3 10.0.0.27
```

The deployment verifier checks all nodes, the client data-plane interface,
leaf BGP convergence, the local gateway, and cross-leaf client reachability.
