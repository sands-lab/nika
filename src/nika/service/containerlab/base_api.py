"""Containerlab lab command API backed by LabRuntime."""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Literal

from nika.runtime.base import LabRuntime
from nika.service.shell import ShellResolver


class ContainerlabBaseAPI:
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

    async def exec_cmd_async(self, host_name: str, command: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.exec_cmd, host_name, command)

    def get_host_ip(self, host_name: str, iface: str = "eth0") -> str | None:
        return self.runtime.get_host_ip(host_name, iface, with_prefix=False)

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
            host_b_ip = self.get_host_ip(host_b)
            if host_b_ip is None:
                return f"Cannot get IP address of host {host_b}."
            host_b = host_b_ip
        command = f"ping -c {count} {host_b} {args}"
        return self.exec_cmd(host_a, command)

    async def _check_ping_success_async(self, host: str, dst_ip: str) -> dict:
        ping_stats_re = re.compile(
            r"(?P<tx>\d+)\s+packets transmitted,\s+"
            r"(?P<rx>\d+)\s+(?:packets\s+)?received,\s+"
            r"(?P<loss>\d+(?:\.\d+)?)%\s+packet loss"
            r"(?:,\s*time\s*(?P<time>\d+)ms)?",
            re.MULTILINE,
        )
        rtt_re = re.compile(
            r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
            r"([\d\.]+)/([\d\.]+)/([\d\.]+)/([\d\.]+)\s*ms",
            re.MULTILINE,
        )
        result = await self.exec_cmd_async(host, f"ping -c 2 -n -q {dst_ip}")
        stats_match = ping_stats_re.search(result)
        tx = rx = loss = time_ms = None
        rtt_min = rtt_avg = rtt_max = rtt_mdev = None
        if stats_match:
            tx = int(stats_match.group("tx"))
            rx = int(stats_match.group("rx"))
            loss = float(stats_match.group("loss"))
            if stats_match.group("time") is not None:
                time_ms = float(stats_match.group("time"))
        rtt_match = rtt_re.search(result)
        if rtt_match:
            rtt_min, rtt_avg, rtt_max, rtt_mdev = map(float, rtt_match.groups())
        if tx is not None and rx is not None and loss is not None:
            if rx > 0 and loss < 100:
                status = "ok"
            elif rx == 0 and loss == 100:
                status = "down"
            else:
                status = "unstable"
        else:
            status = "unknown"
        return {
            "tx": tx,
            "rx": rx,
            "loss_percent": loss,
            "time_ms": time_ms,
            "rtt_min_ms": rtt_min,
            "rtt_avg_ms": rtt_avg,
            "rtt_max_ms": rtt_max,
            "rtt_mdev_ms": rtt_mdev,
            "status": status,
        }

    def _probe_hosts(self) -> list[str]:
        nodes = self.runtime.list_nodes()
        hosts = sorted(
            name
            for name in nodes
            if any(key in name for key in ("client", "pc", "host"))
        )
        return hosts or nodes

    async def get_reachability(self) -> str:
        host_names = self._probe_hosts()
        host_ips = {host_name: self.get_host_ip(host_name) for host_name in host_names}
        host_list = sorted(host_ips.items())
        if len(host_list) > 2:
            dst_list = host_list.copy()
            random.shuffle(dst_list)
            dst_list = dst_list[:2]
        else:
            dst_list = host_list
        coroutines = []
        pairs = []
        for src_name, _ in host_list:
            for dst_name, dst_ip in dst_list:
                if src_name == dst_name or not dst_ip:
                    continue
                pairs.append((src_name, dst_name))
                coroutines.append(self._check_ping_success_async(src_name, dst_ip))
        responses = await asyncio.gather(*coroutines)
        results = []
        for (src, dst), stats in zip(pairs, responses):
            results.append(
                {
                    "src": src,
                    "dst": dst,
                    "dst_ip": host_ips.get(dst),
                    "tx": stats.get("tx"),
                    "rx": stats.get("rx"),
                    "loss_percent": stats.get("loss_percent"),
                    "time_ms": stats.get("time_ms"),
                    "rtt_avg_ms": stats.get("rtt_avg_ms"),
                    "rtt_min_ms": stats.get("rtt_min_ms"),
                    "rtt_max_ms": stats.get("rtt_max_ms"),
                    "rtt_mdev_ms": stats.get("rtt_mdev_ms"),
                    "status": stats.get("status"),
                }
            )
        return json.dumps(
            {"hosts": host_ips, "results": results}, separators=(",", ":")
        )

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
        self.exec_cmd(server_host_name, f"iperf3 -s -D {server_args}")
        server_ip = self.get_host_ip(server_host_name)
        result = self.exec_cmd(
            client_host_name,
            f"iperf3 -c {server_ip} -t {duration} {client_args}",
        )
        self.exec_cmd(server_host_name, "pkill iperf3 2>/dev/null || true")
        return result

    def intf_on_off(
        self, host_name: str, interface: str, state: Literal["up", "down"]
    ) -> str:
        command = f"ip link set {interface} {state}"
        return self.exec_cmd(host_name, command)
