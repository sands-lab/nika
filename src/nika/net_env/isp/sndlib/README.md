# SNDlib ISP topologies

Vendored SNDlib network XML files used by NIKA's backend-agnostic topology import
layer (`nika.topology`).

Each subdirectory contains a single `network.xml` (nodes, links, demands). The
catalog matches the official **all networks** XML archive (26 topologies).
Large dynamic traffic matrix packages are **not** vendored; place normalized
caches under the repo `.nika_cache/sndlib/traffic/<topo>/` (see
`nika traffic fetch sndlib` / ISP README).

## Source

Data from [SNDlib](https://sndlib.put.poznan.pl/home.action) (Poznan University of
Technology / ZIB). Downloaded from the official **all networks** XML archive:
`sndlib-networks-xml`.

When using this data in publications, please cite SNDlib as requested on the
[download page](https://sndlib.put.poznan.pl/download.action).

## Usage

```python
from nika.topology import list_sndlib_topologies, load_sndlib_topology

print(list_sndlib_topologies())
topo = load_sndlib_topology("polska")
# topo.demands → static planning matrix (used by `nika traffic run sndlib --mode demands`)
```

This package only stores topology data. It does not deploy labs by itself.

To run an ISP lab from these topologies (same `isp` scenario):

```bash
uv run nika env run isp --topo polska --igp isis
uv run nika env run isp --backend containerlab --device-profile nokia_srlinux --topo pdh
uv run nika traffic run sndlib --mode demands --unit K --max-intervals 1 --background
```

See [`kathara/isp/isp`](../../kathara/isp/isp/README.md) and
[`containerlab/isp`](../../containerlab/isp/README.md) for backend-specific
details.
