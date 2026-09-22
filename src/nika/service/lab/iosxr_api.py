"""Shared Cisco IOS-XR (XRd) routing API for Kathara labs."""

from __future__ import annotations

import base64
import re

from nika.service.lab.protocols import SupportsExec

CLI_COMMAND = "/pkg/bin/xr_cli '{command}'"

_ASN_FROM_SUMMARY = re.compile(r"local AS number\s+(\d+)", re.IGNORECASE)
_ASN_FROM_RUN = re.compile(r"^router bgp\s+(\d+)\b", re.MULTILINE)
_NETWORK_LINE = re.compile(r"^\s*network\s+(\S+)", re.MULTILINE)
_BANNER_OR_EMPTY = re.compile(
    r"^(Building configuration|!! |% |---+ show |---+$)", re.IGNORECASE
)


def _single_quote(value: str) -> str:
    # nika's exec wrapping over-escapes double quotes (turns them into stray
    # tokens), so nested arguments must stay single-quoted all the way down;
    # this is the standard POSIX close/insert-literal-quote/reopen trick.
    return "'" + value.replace("'", "'\\''") + "'"


class IOSXRAPIMixin:
    """XRd Control Plane operations via ``exec_cmd``."""

    def uses_iosxr_router(self: SupportsExec, device_name: str) -> bool:
        output = self.exec_cmd(
            device_name, "test -x /pkg/bin/xr_cli && echo yes || true"
        )
        return "yes" in output

    def iosxr_exec(
        self: SupportsExec, device_name: str, command: str, timeout: float = 60
    ) -> str:
        return self.exec_cmd(
            device_name, CLI_COMMAND.format(command=command), timeout=timeout
        )

    def iosxr_get_bgp_conf(self: SupportsExec, device_name: str) -> str:
        return self.iosxr_exec(
            device_name, "show running-config router bgp", timeout=90
        )

    def iosxr_show_running_config(self: SupportsExec, device_name: str) -> str:
        return self.iosxr_exec(device_name, "show running-config", timeout=90)

    def iosxr_show_route(self: SupportsExec, device_name: str) -> str:
        return self.iosxr_exec(device_name, "show route", timeout=60)

    def iosxr_apply_config(
        self: SupportsExec, device_name: str, config_lines: list[str]
    ) -> str:
        return self._iosxr_write_apply_file(
            device_name, "/tmp/nika_iosxr_apply.cfg", config_lines
        )

    def iosxr_get_bgp_asn_number(self: SupportsExec, device_name: str) -> int:
        summary = self.iosxr_exec(device_name, "show bgp summary", timeout=90)
        match = _ASN_FROM_SUMMARY.search(summary)
        if match:
            return int(match.group(1))
        conf = self.iosxr_get_bgp_conf(device_name)
        match = _ASN_FROM_RUN.search(conf)
        if match:
            return int(match.group(1))
        full = self.iosxr_show_running_config(device_name)
        match = _ASN_FROM_RUN.search(full)
        if match:
            return int(match.group(1))
        raise RuntimeError(
            f"Could not determine BGP ASN on IOS-XR device {device_name!r}. "
            f"summary={summary!r} conf={conf!r}"
        )

    def iosxr_set_bgp_asn(self: SupportsExec, device_name: str, asn: int) -> None:
        """Replace the local BGP ASN via two-step ``xrapply`` (remove, then recreate).

        Mixing ``no router bgp`` and a new ``router bgp`` in one apply fails on
        XRd (BGP instance race). Apply the remove and recreate as separate files.
        """
        current = self.iosxr_get_bgp_asn_number(device_name)
        if current == asn:
            return
        conf = self.iosxr_get_bgp_conf(device_name)
        rebuilt = _rewrite_router_bgp_asn(conf, current, asn)
        self._iosxr_write_apply_file(
            device_name,
            "/tmp/nika_iosxr_no_bgp.cfg",
            [f"no router bgp {current}", "end"],
        )
        self._iosxr_write_apply_file(
            device_name, "/tmp/nika_iosxr_new_bgp.cfg", [*rebuilt, "end"]
        )

    def _iosxr_write_apply_file(
        self: SupportsExec,
        device_name: str,
        path: str,
        config_lines: list[str],
    ) -> str:
        body = "\n".join(config_lines) + "\n"
        b64 = base64.b64encode(body.encode()).decode("ascii")
        inner = (
            f"echo {_single_quote(b64)} | base64 -d > {_single_quote(path)}; "
            f"source /pkg/bin/ztp_helper.sh; xrapply {_single_quote(path)}"
        )
        command = f"/bin/bash -c {_single_quote(inner)}"
        return self.exec_cmd(device_name, command, timeout=120)

    def iosxr_list_bgp_networks(self: SupportsExec, device_name: str) -> list[str]:
        conf = self.iosxr_get_bgp_conf(device_name)
        return [m.group(1) for m in _NETWORK_LINE.finditer(conf)]

    def iosxr_withdraw_bgp_prefix(
        self: SupportsExec, device_name: str, prefix: str
    ) -> None:
        asn = self.iosxr_get_bgp_asn_number(device_name)
        self._iosxr_write_apply_file(
            device_name,
            "/tmp/nika_iosxr_withdraw.cfg",
            [
                f"router bgp {asn}",
                " address-family ipv4 unicast",
                f"  no network {prefix}",
                " !",
                "!",
                "end",
            ],
        )

    def iosxr_bgp_prefix_withdrawn(
        self: SupportsExec, device_name: str, prefix: str
    ) -> bool:
        networks = self.iosxr_list_bgp_networks(device_name)
        return prefix not in {str(n) for n in networks}


def _rewrite_router_bgp_asn(conf: str, old_asn: int, new_asn: int) -> list[str]:
    """Return BGP config lines with ``router bgp`` ASN replaced."""
    lines: list[str] = []
    replaced = False
    for raw in conf.splitlines():
        stripped = raw.strip()
        if not stripped or _BANNER_OR_EMPTY.match(stripped):
            continue
        if stripped.startswith("%"):
            continue
        if re.match(rf"^router bgp\s+{old_asn}\b", stripped):
            lines.append(f"router bgp {new_asn}")
            replaced = True
            continue
        if stripped == "end":
            continue
        lines.append(raw.rstrip())
    if not replaced:
        lines = [f"router bgp {new_asn}"]
    return lines
