"""Containerlab lab command API backed by LabRuntime."""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from nika.runtime.base import LabRuntime
from nika.service.lab.reachability import PingReachabilityMixin
from nika.service.shell import ShellResolver, iperf_server_commands, ping_exec_timeout


class ContainerlabBaseAPI(PingReachabilityMixin):
    """Host exec API compatible with KatharaBaseAPI callers for Containerlab labs."""

    backend = "containerlab"

    def __init__(self, runtime: LabRuntime) -> None:
        self.runtime = runtime
        self.lab_name = runtime.lab_name
        self._shell = ShellResolver()

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        return self._shell.exec_via_shell(
            host_name,
            command,
            self.runtime.exec,
            timeout=timeout,
        )

    async def exec_cmd_async(
        self, host_name: str, command: str, timeout: float = 10
    ) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.exec_cmd(host_name, command, timeout=timeout)
        )

    def get_host_ip(self, host_name: str, iface: str = "eth0") -> str | None:
        return self.runtime.get_host_ip(host_name, iface, with_prefix=False)

    def _data_plane_ip(self, host_name: str) -> str | None:
        # eth0 on Containerlab nodes is the management network; probes must
        # target the lab data plane.
        return self.runtime.get_data_plane_host_ip(host_name, with_prefix=False)

    def get_host_net_config(self, host_name: str) -> dict:
        return {
            "host_name": host_name,
            "ifconfig": self.exec_cmd(host_name, "ifconfig -a 2>/dev/null || ip addr"),
            "ip_addr": self.exec_cmd(host_name, "ip addr"),
            "ip_route": self.exec_cmd(host_name, "ip route"),
        }

    def ping_pair(
        self, host_a: str, host_b: str, count: int = 4, args: str = ""
    ) -> str:
        ip_re = r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
        if not re.match(ip_re, host_b):
            host_b_ip = self._data_plane_ip(host_b)
            if host_b_ip is None:
                return f"Cannot get IP address of host {host_b}."
            host_b = host_b_ip
        command = f"ping -c {count} {host_b} {args}"
        # Unanswered pings linger ~10s after the last probe; budget for it so a
        # black-holed path reports 100% loss instead of a timeout.
        return self.exec_cmd(host_a, command, timeout=ping_exec_timeout(count))

    def _probe_hosts(self) -> list[str]:
        nodes = self.runtime.list_nodes()
        hosts = sorted(
            name
            for name in nodes
            if any(key in name for key in ("client", "pc", "host"))
        )
        return hosts or nodes

    async def get_reachability(self) -> str:
        host_ips = {
            host_name: self._data_plane_ip(host_name)
            for host_name in self._probe_hosts()
        }
        return await self._sampled_reachability(host_ips)

    def systemctl_ops(
        self,
        host_name: str,
        service_name: str,
        operation: Literal["start", "stop", "restart", "status"],
    ) -> str:
        return self.exec_cmd(host_name, f"systemctl {operation} {service_name}")

    def netstat(self, host_name: str, args: str = "-tuln") -> str:
        return self.exec_cmd(host_name, f"netstat {args}")

    def ip_addr_statistics(self, host_name: str) -> str:
        return self.exec_cmd(host_name, "ip -s addr")

    def ethtool(self, host_name: str, interface: str, args: str = "") -> str:
        return self.exec_cmd(host_name, f"ethtool {interface} {args}")

    def tc_show_statistics(self, host_name: str, intf_name: str) -> str:
        return self.exec_cmd(host_name, f"tc -s qdisc show dev {intf_name}")

    def curl_web_test(self, host_name: str, url: str, times: int = 5) -> str:
        command = (
            f"curl --connect-timeout 5 --max-time 10 "
            f"-w 'namelookup:%{{time_namelookup}}, "
            f"connect:%{{time_connect}}, "
            f"appconnect:%{{time_appconnect}}, "
            f"pretransfer:%{{time_pretransfer}}, "
            f"starttransfer:%{{time_starttransfer}}, "
            f"total:%{{time_total}}\\n' "
            f"-o /dev/null -s {url}"
        )
        res = ""
        for _ in range(times):
            res += self.exec_cmd(host_name, command) + "\n"
        return res.strip()

    def iperf_test(
        self,
        client_host_name: str,
        server_host_name: str,
        duration: int = 10,
        client_args: str = "",
        server_args: str = "",
    ) -> str:
        start, stop = iperf_server_commands(server_args)
        self.exec_cmd(server_host_name, start)
        server_ip = self._data_plane_ip(server_host_name)
        result = self.exec_cmd(
            client_host_name,
            f"iperf3 -c {server_ip} -t {duration} {client_args}",
            timeout=duration + 15,
        )
        self.exec_cmd(server_host_name, stop)
        return result

    def intf_on_off(
        self, host_name: str, interface: str, state: Literal["up", "down"]
    ) -> str:
        command = f"ip link set {interface} {state}"
        return self.exec_cmd(host_name, command)
