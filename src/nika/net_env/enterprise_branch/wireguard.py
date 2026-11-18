"""WireGuard keys and restored handshakes for enterprise_branch edge tunnels."""

from __future__ import annotations

from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING

from nika.config import pkg_path

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime


def resume_peer_handshakes(
    runtime: LabRuntime, node: str, tunnels: list[tuple[str, str]]
) -> None:
    """Separate simultaneous handshakes on this edge's point-to-point tunnels."""
    probes = "\n".join(
        f"(ping -n -q -I {quote(interface)} -c 1 -W 1 {quote(peer_ip)} "
        ">/dev/null 2>&1 || true) &"
        for interface, peer_ip in tunnels
    )
    output = runtime.exec(
        node,
        """set -eu
export LC_ALL=C
ports=""
interfaces=$(wg show interfaces)
for interface in $interfaces; do
    local_key=$(wg show "$interface" public-key)
    peers=$(wg show "$interface" peers)
    for peer in $peers; do
        if [ "$local_key" \\> "$peer" ]; then
            ports="$ports $(wg show "$interface" listen-port)"
            break
        fi
    done
done
table=nika_wg_restore_$$
created=0
cleanup() {
    if [ "$created" = 1 ]; then
        nft delete table inet "$table"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
if [ -n "$ports" ]; then
    nft create table inet "$table"
    created=1
    nft "add chain inet $table output { type filter hook output priority -50; policy accept; }"
    for port in $ports; do
        # WireGuard message type 1 is little-endian, after the UDP header.
        nft add rule inet "$table" output udp sport "$port" @th,64,32 0x01000000 drop
    done
fi
"""
        + probes
        + """
wait
# Allow a handshake retry (five seconds plus jitter) while one end responds.
sleep 8
cleanup
created=0
echo NIKA_WG_RESUMED
""",
        timeout=30,
    )
    if "NIKA_WG_RESUMED" not in output.splitlines():
        raise RuntimeError(f"WireGuard handshake recovery failed on {node}")


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
