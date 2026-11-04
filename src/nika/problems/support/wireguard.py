"""WireGuard peer operations on Linux routers that netlab configures.

netlab configures Linux WireGuard interfaces with a ``wg set`` script that the
router runs at startup. A peer change applies to the running interface and to
that script, so the change persists like an edit to the router's
configuration. The methods match the tunnel operations of
:class:`nika.problems.support.router_ops.RouterFaultOps`.
"""

from __future__ import annotations

import base64
import re
import time

from nika.net_env.synthesis.configuration import NETLAB_SCRIPT_DIR
from nika.problems.support.router_ops import TunnelPeer

SCRIPT = f"{NETLAB_SCRIPT_DIR}/tunnel.wireguard.sh"


class LinuxWireGuard:
    def __init__(self, runtime, node: str):
        self.runtime = runtime
        self.node = node

    def interfaces(self) -> list[str]:
        out = self.runtime.exec(self.node, "wg show interfaces 2>/dev/null || true")
        return sorted(out.split())

    def _peer_row(self, interface: str) -> list[str]:
        # ``wg show <if> dump``: the interface line, then one line per peer:
        # public-key preshared-key endpoint allowed-ips latest-handshake rx tx keepalive
        rows = [
            line.split("\t")
            for line in self.runtime.exec(
                self.node, f"wg show {interface} dump 2>/dev/null || true"
            ).splitlines()[1:]
            if line.strip()
        ]
        if len(rows) != 1:
            raise ValueError(f"Expected one WireGuard peer on {self.node}:{interface}")
        return rows[0]

    def _edit_script(self, interface: str, old: str, new: str) -> None:
        """Replace ``old`` with ``new`` in the script's block for ``interface``."""
        script = self.runtime.exec(self.node, f"cat {SCRIPT}")
        block = re.search(
            rf"^wg set {re.escape(interface)} \\\n.*?^ip link set dev {re.escape(interface)} up$",
            script,
            re.M | re.S,
        )
        if block is None or block[0].count(old) != 1:
            raise ValueError(f"No WireGuard peer for {interface} in {SCRIPT}")
        edited = script.replace(block[0], block[0].replace(old, new))
        encoded = base64.b64encode(edited.encode()).decode()
        self.runtime.exec(self.node, f"echo {encoded} | base64 -d > {SCRIPT}")

    def tunnel_peer(self, interface: str) -> TunnelPeer:
        row = self._peer_row(interface)
        allowed = tuple(a for a in row[3].split(",") if a and a != "(none)")
        handshake = int(row[4])
        age = time.time() - handshake if handshake else None
        return TunnelPeer(row[0], allowed, age)

    def set_tunnel_peer_public_key(self, interface: str, key: str) -> None:
        """Replace the peer's key, keeping its endpoint, allowed IPs and keepalive."""
        old, _psk, endpoint, allowed, _hs, _rx, _tx, keepalive = self._peer_row(
            interface
        )
        command = f"wg set {interface} peer {old} remove peer {key}"
        if allowed != "(none)":
            command += f" allowed-ips {allowed}"
        if endpoint != "(none)":
            command += f" endpoint {endpoint}"
        if keepalive != "off":
            command += f" persistent-keepalive {keepalive}"
        self.runtime.exec(self.node, command)
        self._edit_script(interface, f"peer {old} ", f"peer {key} ")

    def set_tunnel_peer_allowed_addresses(
        self, interface: str, prefixes: list[str]
    ) -> None:
        key, _psk, _endpoint, allowed = self._peer_row(interface)[:4]
        value = ",".join(prefixes)
        self.runtime.exec(
            self.node, f"wg set {interface} peer {key} allowed-ips {value}"
        )
        self._edit_script(interface, f"allowed-ips {allowed} ", f"allowed-ips {value} ")
