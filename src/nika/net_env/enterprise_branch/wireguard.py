"""WireGuard config helpers for enterprise_branch edge tunnels."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nika.config import pkg_path

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime


def restore_peer_sessions(runtime: LabRuntime, node: str) -> None:
    """Rebuild ephemeral peer sessions from the running WireGuard configuration."""
    output = runtime.exec(
        node,
        """set -eu
umask 077
state_file=$(mktemp)
trap 'rm -f "$state_file"' EXIT HUP INT TERM
interfaces=$(wg show interfaces)
for interface in $interfaces; do
    wg showconf "$interface" > "$state_file"
    peers=$(wg show "$interface" peers)
    for peer in $peers; do
        wg set "$interface" peer "$peer" remove
    done
    wg setconf "$interface" "$state_file"
done
echo NIKA_WG_RESTORED
""",
        timeout=30,
    )
    if "NIKA_WG_RESTORED" not in output.splitlines():
        raise RuntimeError(f"WireGuard session recovery failed on {node}")


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
