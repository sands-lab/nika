# ISP (SNDlib → Containerlab / Nokia SR Linux)

Same `isp` scenario as Kathara, selected with:

```bash
uv run nika env run isp --backend containerlab --device-profile nokia_srlinux
uv run nika env run isp --backend containerlab --topo pdh --igp isis
uv run nika env run isp --backend containerlab --topo polska --bgp-mode ibgp_rr
```

Defaults: `--backend containerlab` implies `--device-profile nokia_srlinux`.

Each SNDlib node becomes a `nokia_srlinux` router; each link is a point-to-point
`/31` on `e1-N`. Edge stub hosts `pc_<router>` (linux / multitool) are always
attached for `nika traffic run sndlib`.

## Traffic

```bash
uv run nika traffic run sndlib --mode demands --unit K --max-intervals 1 --background
```

Uses the same shared traffic IR and stub inventory as Kathara ISP.

## Addressing

| Resource | Scheme |
|----------|--------|
| Device name | SNDlib slug (`Gdansk` → `gdansk`) |
| Router ifaces | Plan `ethN` → Containerlab `e1-{N+1}` / SRL `ethernet-1/{N+1}` |
| Stub host data iface | `eth1` |
| Loopback | `system0` `/32` in `10.255.0.0/16` |

## Shared compilers

- Plan / IGP: `nika.net_env.isp.igp`
- BGP presets: `nika.net_env.isp.bgp`
- SRL render: `nika.net_env.isp.igp.srl`, `nika.net_env.isp.bgp.srl`
- Traffic: `nika.net_env.isp.traffic`
- Data: `nika.net_env.isp.sndlib`

See also `src/nika/net_env/kathara/isp/isp/README.md` for Kathara/`frr` details.
