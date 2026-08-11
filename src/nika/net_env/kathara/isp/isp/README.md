# ISP (SNDlib → Kathara / FRR)

Runnable ISP backbone scenario compiled from vendored
[SNDlib](https://sndlib.put.poznan.pl/home.action) topologies.

Each SNDlib node becomes an FRR router; each SNDlib link becomes a point-to-point
`/31` attachment. SNDlib defines **physical topology only**. Optional BGP control
plane comes from NIKA presets (`--bgp-mode`), never from SNDlib metadata.

## Quick start

```bash
# IGP only (defaults: --topo polska --igp isis --bgp-mode none --backend kathara)
# Edge stub hosts pc_<router> are always attached for traffic replay.
uv run nika env run isp

uv run nika env run isp --topo abilene --igp isis
uv run nika env run isp --topo polska --igp ospf

# BGP modes (NIKA presets; same physical topo)
uv run nika env run isp --topo polska --bgp-mode ibgp_rr
uv run nika env run isp --topo geant --bgp-mode ebgp --igp isis

# Containerlab / Nokia SR Linux (same scenario)
uv run nika env run isp --backend containerlab --device-profile nokia_srlinux --topo pdh

# Replay SNDlib traffic (choose matrix at traffic start, not env run)
uv run nika traffic run sndlib --mode demands --unit K --max-intervals 1 --background

uv run nika session close -y
```

List catalog names:

```bash
uv run python -c "from nika.topology import list_sndlib_topologies as t; print(t())"
```

## Addressing and naming

| Resource | Scheme |
|----------|--------|
| Device name | Stable slug of SNDlib node id (`Gdansk` → `gdansk`) |
| Loopback | `10.255.0.0/16`, one `/32` per router (sorted node order) |
| Router-id | Same as loopback IPv4 |
| P2P links | `10.0.0.0/8` carved into `/31`s (sorted link order) |
| Collision domain | `cd_<slug(link_id)>` |
| Stub host | `pc_<router>` on `cd_edge_<router>`; LAN `10.254.0.0/16` `/30` (router `.1`, host `.2`) |
| IS-IS NET | `49.0001.<sysid>.00` derived from router-id |
| OSPF | Single area `0.0.0.0` (NBMA + explicit neighbors) |
| Business prefixes (BGP) | Lab TEST-NET ranges (see presets); not from SNDlib demands |

## IGP modes

| `--igp` | Behavior |
|---------|----------|
| `isis` (default) | FRR `isisd`, level-2-only, wide metrics, passive loopback |
| `ospf` | FRR `ospfd`, single area 0, NBMA + explicit neighbors |

## Metric strategies

| `--metric-strategy` | Behavior |
|---------------------|----------|
| `constant` (default) | Every link uses `--constant-metric` (default **10**) |
| `routing_cost` | `round(link.routing_cost)`; missing/invalid → constant |
| `inv_capacity` | `max(1, round(1_000_000 / preinstalled_capacity))`; missing → constant |

## BGP presets (`--bgp-mode`)

BGP ASN, RR roles, sessions, business prefixes, and route-maps are **NIKA
presets**. They use sorted ISP device names and ISP links only — they
do not infer AS borders from SNDlib geography or demands.

| `--bgp-mode` | Behavior |
|--------------|----------|
| `none` (default) | IGP only; `bgpd` off |
| `ibgp_rr` | Single ASN `65000`. First `min(2,n)` sorted devices are RRs; rest are clients. iBGP over loopbacks; RR–RR mesh; clients peer every RR. Last `min(3,n)` devices originate `203.0.113/114/115.0/24`. Export/import route-maps allow only business prefixes. |
| `ebgp` | Partition sorted devices into `min(3,n)` ASes (`65001..`). eBGP only on ISP links that cross AS boundaries (P2P IPs). No iBGP in this preset (intra-AS reachability stays IGP-only). Each AS originates one `198.51.10x.0/24` on its last border router. Verify observers are direct eBGP peers of the originator. eBGP requires in/out route-maps; infra `10.0.0.0/8` and `10.255.0.0/16` denied. |

Infra loopbacks and P2P addresses are never advertised into BGP.

## Traffic (`nika traffic run sndlib`)

`env run isp` always attaches edge stub hosts (`pc_<router>` on every router)
with IGP-passive `/30` edges. Choose the matrix when starting traffic:

| `--mode` | Behavior |
|----------|----------|
| `demands` (default) | One interval from vendored `network.xml` `<demands>` |
| `dynamic` | Load `.nika_cache/sndlib/traffic/<topo>/` if present; otherwise **fall back to demands** with a warning |

`--scale` (default `1.0`) multiplies rates before iperf `-b`. SNDlib units
are **not** Mbps.

```bash
# Optional: place/fetch normalized dynamic cache
uv run nika traffic fetch sndlib --topo abilene   # when URL/adapter configured
# or write .nika_cache/sndlib/traffic/<topo>/{manifest.json,intervals/000000.json}

uv run nika env run isp --topo abilene
uv run nika traffic run sndlib --mode dynamic --background --max-intervals 3 --unit K
```

Replay walks intervals **in order**. Traffic does **not** auto-start on `env run`.

## Runtime inventory

`Isp.inventory` maps SNDlib entities to devices/addresses (and nested
`bgp` / `hosts` / `traffic`). In-process only; used by verify.

## Architecture

- ISP plan compiler (topology + IGP): `nika.net_env.isp.igp`
- BGP compiler: `nika.net_env.isp.bgp`
- Traffic series / stubs: `nika.net_env.isp.traffic`
- Data: `nika.net_env.isp.sndlib`
- Kathara binder: this directory (`lab.py`, `verify.py`)

## Verification

Always:

1. Planned routers deployed; FRR active
2. IGP adjacencies match link degree (backbone links only)
3. Loopbacks reachable (spanning tree)
4. Inventory address spot-checks
5. Stub hosts addressed; gateway ping; one remote stub ping

When `--bgp-mode` ≠ `none`:

6. Planned BGP sessions Established
7. Originators have local business prefixes in BGP
8. Configured observers learn prefixes and can ping the originator host in-prefix
9. Infra prefixes do not appear in the BGP unicast table

Integration tests deploy every catalog topology for IGP (`isis`/`ospf`) and for
BGP (`ibgp_rr`/`ebgp`). Docker also covers `pdh`/`polska`/`abilene` demands
replay and fixture-backed dynamic replay on `polska`/`abilene`.

## Compatible failures

Problems whose `TAGS` are a subset of this scenario’s tags (`link`, `icmp`,
`frr`, `ospf`, `bgp`, …) can be injected on `isp`. Devices are city
slugs from inventory — never `pc1`.

| Group | Failures | Required lab mode |
|-------|----------|-------------------|
| link | `link_down`, `link_flap`, `link_detach`, `link_fragmentation_disabled`, `link_bandwidth_throttling`, `link_high_packet_corruption` | any `--igp` / `--bgp-mode` |
| frr | `frr_service_down` | any |
| icmp | `icmp_acl_block` | any |
| ospf | `ospf_area_misconfiguration`, `ospf_neighbor_missing`, `ospf_acl_block` | `--igp ospf` (any BGP mode) |
| bgp | `bgp_asn_misconfig`, `bgp_acl_block`, `bgp_missing_route_advertisement`, `bgp_blackhole_route_leak`, `host_static_blackhole`, `bgp_hijacking` | `--bgp-mode ibgp_rr` or `ebgp` (any IGP) |

Examples:

```bash
uv run nika env run isp --topo polska --igp isis
uv run nika failure inject link_down --set host_name=bialystok --set intf_name=eth0

uv run nika env run isp --topo polska --igp ospf
uv run nika failure inject ospf_neighbor_missing --set host_name=bialystok

uv run nika env run isp --topo geant --bgp-mode ibgp_rr
uv run nika failure inject bgp_asn_misconfig --set host_name=<originator>
uv run nika failure inject bgp_hijacking --set host_name=<speaker> --set target_network=198.18.0.0/24
```

`bgp_blackhole_route_leak` / `host_static_blackhole` resolve a victim as a
**neighbor router** on this router-only lab (no PCs). Prefer an originator as
`host_name`. `bgp_hijacking` needs an explicit `target_network` (no web/pc
defaults). Docker e2e covers these failures on `polska` and `geant` across all
applicable IGP/BGP mode combinations.

## Citation

When publishing results that use these topologies, cite SNDlib as requested on
the [SNDlib download page](https://sndlib.put.poznan.pl/download.action).
