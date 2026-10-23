"""IOS XR (XRd) provider for failure operations on native routers."""

from __future__ import annotations

from nika.net_env.utils.iosxr.common import build_xr_file_command
from nika.problems.support.router_ops import RouterFaultOps

# XR commits and configuration replacement can take minutes on a loaded host.
_CONFIG_TIMEOUT_SEC = 300


def _replace_command(running: str) -> str:
    """Replace the running configuration with ``show running-config`` output."""
    lines = running.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("!! IOS XR")), None
    )
    if start is None or not any(line == "end" for line in lines[start:]):
        raise ValueError("Unrecognized IOS XR running configuration")
    config = "\n".join(lines[start:]) + "\n"
    return build_xr_file_command(config, 'xrreplace "$xr_config_file"')


class IosxrFaultOps(RouterFaultOps):
    profile = "iosxr"

    def bgp_asn(self) -> int:
        return self.runtime.iosxr_get_bgp_asn_number(self.node)

    def set_bgp_asn(self, asn: int) -> None:
        self.runtime.iosxr_set_bgp_asn(self.node, asn)

    def bgp_networks(self) -> list[str]:
        return self.runtime.iosxr_list_bgp_networks(self.node)

    def withdraw_bgp_network(self, prefix: str) -> None:
        self.runtime.iosxr_withdraw_bgp_prefix(self.node, prefix)

    def _exec(self, command: str) -> str:
        result = self.runtime.exec(self.node, command, timeout=_CONFIG_TIMEOUT_SEC)
        if result.startswith("[TIMEOUT]"):
            raise RuntimeError(
                f"IOS XR configuration operation timed out on {self.node}"
            )
        return result

    def save_config(self) -> str:
        return self._exec("/pkg/bin/xr_cli 'show running-config'")

    def restore_config(self, saved: str) -> None:
        self._exec(_replace_command(saved))
