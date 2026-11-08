"""WireGuard key pairs for enterprise_branch edge tunnels."""

from __future__ import annotations

from pathlib import Path

from nika.config import pkg_path


def load_key_pairs(path: Path | None = None) -> list[tuple[str, str]]:
    keys_path = path or pkg_path("net_env/utils/kathara/wireguard/keys.txt")
    pairs: list[tuple[str, str]] = []
    for line in keys_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        priv, pub = line.split(",", 1)
        pairs.append((priv.strip(), pub.strip()))
    if len(pairs) < 10:
        raise RuntimeError(
            f"Need at least 10 WireGuard key pairs in {keys_path}, found {len(pairs)}"
        )
    return pairs
