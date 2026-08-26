"""Shared Cisco IOS-XR (XRd) routing API for Kathara labs."""

from __future__ import annotations

from nika.service.lab.protocols import SupportsExec

CLI_COMMAND = "/pkg/bin/xr_cli '{command}'"


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

    def iosxr_exec(self: SupportsExec, device_name: str, command: str) -> str:
        return self.exec_cmd(device_name, CLI_COMMAND.format(command=command))

    def iosxr_get_bgp_conf(self: SupportsExec, device_name: str) -> str:
        return self.iosxr_exec(device_name, "show running-config router bgp")

    def iosxr_show_running_config(self: SupportsExec, device_name: str) -> str:
        return self.iosxr_exec(device_name, "show running-config")

    def iosxr_show_route(self: SupportsExec, device_name: str) -> str:
        return self.iosxr_exec(device_name, "show route")

    def iosxr_apply_config(
        self: SupportsExec, device_name: str, config_lines: list[str]
    ) -> str:
        content = "\\n".join(config_lines)
        inner = (
            f"source /pkg/bin/ztp_helper.sh; xrapply_string {_single_quote(content)}"
        )
        command = f"/bin/bash -c {_single_quote(inner)}"
        return self.exec_cmd(device_name, command)
