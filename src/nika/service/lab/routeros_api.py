"""Shared MikroTik RouterOS routing API for Kathara labs."""

from __future__ import annotations

from nika.service.lab.protocols import SupportsExec

# RouterOS's mgmt IP lives on vrnetlab's internal bridge, reachable only from
# inside the container's network namespace. `vrnetlab`/`VR-netlab9` are
# vrnetlab's own fixed, non-secret internal defaults for that loopback-only
# segment, not real credentials protecting anything outside the container.
MGMT_USER = "vrnetlab"
MGMT_PASSWORD = "VR-netlab9"
MGMT_ADDR = "172.31.255.30"

CLI_COMMAND = (
    f"sshpass -p {MGMT_PASSWORD} ssh -q -oStrictHostKeyChecking=no "
    f"-oConnectTimeout=1 {MGMT_USER}@{MGMT_ADDR} '{{command}}'"
)


class RouterOSAPIMixin:
    """RouterOS operations via ``exec_cmd`` over an internal, docker-exec-only SSH hop."""

    def uses_routeros_router(self: SupportsExec, device_name: str) -> bool:
        output = self.exec_cmd(device_name, "which sshpass && echo yes || true")
        return "yes" in output

    def routeros_exec(self: SupportsExec, device_name: str, command: str) -> str:
        # RouterOS CLI values needed here (IPs, interface names, AS numbers)
        # never contain spaces, so commands stay free of embedded double
        # quotes — nika's exec wrapping mis-escapes those.
        return self.exec_cmd(device_name, CLI_COMMAND.format(command=command))

    def routeros_show_interfaces(self: SupportsExec, device_name: str) -> str:
        return self.routeros_exec(device_name, "/interface print")

    def routeros_get_bgp_conf(self: SupportsExec, device_name: str) -> str:
        return self.routeros_exec(device_name, "/routing bgp connection print detail")

    def routeros_show_route(self: SupportsExec, device_name: str) -> str:
        return self.routeros_exec(device_name, "/ip route print")

    def routeros_apply_config(
        self: SupportsExec, device_name: str, config_lines: list[str]
    ) -> str:
        # RouterOS has no single-shot config-blob apply like XRd's
        # xrapply_string; chain the discrete CLI commands in one SSH hop.
        command = "; ".join(config_lines)
        return self.routeros_exec(device_name, command)
