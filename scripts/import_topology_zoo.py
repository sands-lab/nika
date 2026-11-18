#!/usr/bin/env python3
"""Vendor Internet Topology Zoo GraphML files for ISP scenarios.

Downloads the official Topology Zoo archive, drops topologies that duplicate a
vendored SNDlib topology or have more than ``MAX_ROUTERS`` routers, copies the rest unchanged to
``src/nika/net_env/isp/topologies/<slug>.graphml`` and prints the
``ZOO_TOPOLOGY_TIERS`` table for ``nika.topology.catalog``.

Usage: uv run python scripts/import_topology_zoo.py [--archive PATH]
"""

from __future__ import annotations

import argparse
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

from nika.topology.catalog import (
    SNDLIB_TOPOLOGY_NAMES,
    ZOO_EXCLUDED_DUPLICATES,
    load_topology,
    topology_data_root,
)
from nika.topology.parse_graphml import parse_zoo_graphml

ARCHIVE_URLS = (
    "http://www.topology-zoo.org/files/archive.zip",
    # Wayback copy of the same archive, used when the site is down.
    "http://web.archive.org/web/20260304173450id_/"
    "https://topology-zoo.org/files/archive.zip",
)
# Router-count boundaries of SNDLIB_TOPOLOGY_TIERS; larger topologies are "l".
TIER_MAX_NODES = (("s", 15), ("m", 28))
# Each router also gets a stub host, so Kathara runs 2x this many containers.
# Only KDL (754 routers) is above the cap; the next largest has 197.
MAX_ROUTERS = 200


def _download() -> bytes:
    for url in ARCHIVE_URLS:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except OSError as exc:
            print(f"download failed: {url}: {exc}", file=sys.stderr)
    raise SystemExit("could not download the Topology Zoo archive")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, help="local archive.zip")
    args = parser.parse_args()

    payload = args.archive.read_bytes() if args.archive else _download()
    archive = zipfile.ZipFile(io.BytesIO(payload))

    sndlib_shapes = {
        name: (len(topo.nodes), len(topo.links))
        for name in SNDLIB_TOPOLOGY_NAMES
        for topo in [load_topology(name)]
    }
    out = topology_data_root()
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.graphml"):
        stale.unlink()

    tiers: dict[str, list[str]] = {"s": [], "m": [], "l": []}
    for member in sorted(archive.namelist()):
        if not member.endswith(".graphml"):
            continue
        slug = Path(member).stem.lower()
        raw = archive.read(member)
        topo = parse_zoo_graphml(raw.decode("utf-8"), name=slug)
        shape = (len(topo.nodes), len(topo.links))
        same_shape = [name for name, other in sndlib_shapes.items() if other == shape]
        if slug in SNDLIB_TOPOLOGY_NAMES or slug in ZOO_EXCLUDED_DUPLICATES:
            print(f"skip {slug}: duplicates an SNDlib topology", file=sys.stderr)
            continue
        if shape[0] > MAX_ROUTERS:
            print(f"skip {slug}: {shape[0]} routers > {MAX_ROUTERS}", file=sys.stderr)
            continue
        if same_shape:
            print(
                f"review {slug}: same node/link counts as SNDlib {same_shape}; "
                "add to ZOO_EXCLUDED_DUPLICATES if it is the same network",
                file=sys.stderr,
            )
        (out / f"{slug}.graphml").write_bytes(raw)
        tier = next(
            (tier for tier, max_nodes in TIER_MAX_NODES if shape[0] <= max_nodes), "l"
        )
        tiers[tier].append(slug)

    print("ZOO_TOPOLOGY_TIERS: dict[str, tuple[str, ...]] = {")
    for tier, names in tiers.items():
        print(f'    "{tier}": (')
        for name in sorted(names):
            print(f'        "{name}",')
        print("    ),")
    print("}")


if __name__ == "__main__":
    main()
