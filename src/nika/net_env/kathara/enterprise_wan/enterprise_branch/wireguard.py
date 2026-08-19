"""WireGuard config helpers for enterprise_branch edge tunnels."""

from __future__ import annotations

from pathlib import Path

from nika.config import pkg_path


def load_key_pairs(path: Path | None = None) -> list[tuple[str, str]]:
    keys_path = path or pkg_path("net_env/kathara/utils/wireguard/keys.txt")
    pairs: list[tuple[str, str]] = []
    for line in keys_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        priv, pub = line.split(",", 1)
        pairs.append((priv.strip(), pub.strip()))
    if len(pairs) < 9:
        raise RuntimeError(
            f"Need at least 9 WireGuard key pairs in {keys_path}, found {len(pairs)}"
        )
    return pairs


def render_wg_conf(
    *,
    private_key: str,
    address_cidr: str,
    listen_port: int,
    peer_public_key: str,
    peer_endpoint: str,
    keepalive: int = 25,
) -> str:
    """Site-to-site WG with Table=off so FRR owns overlay routes."""
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {address_cidr}\n"
        f"ListenPort = {listen_port}\n"
        "Table = off\n"
        "MTU = 1420\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {peer_public_key}\n"
        f"Endpoint = {peer_endpoint}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        f"PersistentKeepalive = {keepalive}\n"
    )
