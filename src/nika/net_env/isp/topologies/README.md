# Vendored ISP topologies

Each file backs the ISP scenario `isp_<name>`. `<name>.xml` files come from
[SNDlib](https://sndlib.put.poznan.pl/) (`sndlib-networks-xml`), and
`<name>.graphml` files come from the Internet Topology Zoo.

## Topology Zoo

The `.graphml` files are the processed GraphML files from the
[Internet Topology Zoo](http://www.topology-zoo.org/) archive
(`files/archive.zip`, dataset last updated 2013). They are stored unchanged.
`Abilene` is not included because SNDlib already provides `isp_abilene`, and
`KDL` (754 routers) is not included because it is too large for one host.

To regenerate the files and the `ZOO_TOPOLOGY_TIERS` table in
`nika.topology.catalog`, run:

```bash
uv run python scripts/import_topology_zoo.py
```

The Topology Zoo site does not state a data license. It asks users of the
dataset to cite:

> S. Knight, H. X. Nguyen, N. Falkner, R. Bowden and M. Roughan,
> "The Internet Topology Zoo," IEEE Journal on Selected Areas in
> Communications, vol. 29, no. 9, pp. 1765-1775, October 2011.
