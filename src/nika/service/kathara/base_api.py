import asyncio
import json
import random
import re
import time
from collections import defaultdict
from typing import Dict, Literal, Optional, Protocol, runtime_checkable

from func_timeout import func_timeout
from func_timeout.exceptions import FunctionTimedOut
from Kathara.exceptions import MachineNotFoundError
from Kathara.manager.docker.stats.DockerLinkStats import DockerLinkStats
from Kathara.manager.Kathara import Kathara, Lab
from Kathara.model.Machine import Machine


@runtime_checkable
class _SupportsBase(Protocol):
    instance: "Kathara"
    lab: "Lab"

    def _run_cmd(self, host_name: str, command: str) -> str: ...


class KatharaBaseAPI:
    """
    Base interfaces to interact with the Kathara.
    """

    def __init__(self, lab_name: str):
        self.instance = Kathara.get_instance()
        self.lab = self.instance.get_lab_from_api(lab_name=lab_name)
        if self.lab is None:
            raise ValueError(f"Lab {lab_name} not found.")
        self._resolved_shell_cache: dict[str, str] = {}

    def _get_lab_link_stats(self) -> Dict[str, DockerLinkStats]:
        """Get the link stats of the lab."""
        return next(self.instance.get_links_stats(lab_name=self.lab.name))

    @staticmethod
    def _escape_for_shell_c(command: str) -> str:
        return command.replace("'", "'\\''").replace('"', '\\"')

    def _wrap_shell_command(self, shell: str, command: str) -> str:
        escaped = self._escape_for_shell_c(command)
        return f"{shell} -c '{escaped}'"

    def _resolve_shell(self, host_name: str) -> str:
        """Pick bash or sh for ``host_name`` (cached per API instance)."""
        cached = self._resolved_shell_cache.get(host_name)
        if cached is not None:
            return cached

        machine = self.lab.machines.get(host_name)
        if machine is not None and "shell" in machine.meta:
            shell = machine.get_shell()
            self._resolved_shell_cache[host_name] = shell
            return shell

        probe_cmd = (
            "/bin/sh -c 'if [ -x /bin/bash ]; then echo /bin/bash; "
            "elif [ -x /bin/sh ]; then echo /bin/sh; else echo /bin/sh; fi'"
        )
        probed = self._run_cmd(host_name, probe_cmd).strip()
        shell = probed if probed in ("/bin/bash", "/bin/sh") else "/bin/sh"
        self._resolved_shell_cache[host_name] = shell
        return shell

    # Sentinel returned to AGENT-FACING callers on exec timeout (the agent can
    # reason about it). Internal orchestration code must never treat it as
    # data — use exec_cmd_checked instead.
    TIMEOUT_SENTINEL = "[TIMEOUT]"

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        """
        Run a command on a machine and return its output as a string.
        """
        cmd_timeout = timeout
        shell = self._resolve_shell(host_name)
        cmd = self._wrap_shell_command(shell, command)
        try:
            return func_timeout(cmd_timeout, self._run_cmd, args=(host_name, cmd))
        except FunctionTimedOut:
            return f"{self.TIMEOUT_SENTINEL} Command '{command}' on '{host_name}' exceeded {cmd_timeout}s."

    def exec_cmd_checked(
        self,
        host_name: str,
        command: str,
        timeout: float = 10,
        retries: int = 2,
        retry_delay: float = 5.0,
    ) -> str:
        """exec_cmd for ORCHESTRATION callers: retries transient timeouts and
        raises instead of returning the ``[TIMEOUT]`` sentinel as data.

        Right after a (re)deploy a container can be slow for a few seconds;
        without this, the sentinel string leaks into JSON parsers and
        verification comparisons and produces misleading downstream errors.
        """
        last_output = ""
        for attempt in range(retries + 1):
            output = self.exec_cmd(host_name, command, timeout=timeout)
            if not output.startswith(self.TIMEOUT_SENTINEL):
                return output
            last_output = output
            if attempt < retries:
                time.sleep(retry_delay)
        raise RuntimeError(
            f"Command on '{host_name}' kept timing out after {retries + 1} "
            f"attempts ({timeout}s each): {last_output}"
        )

    def get_hosts(self) -> list[Machine]:
        """
        Get the list of hosts (all containers with Docker image kathara/base) in the lab.
        """
        hosts = []
        for name, machine in self.lab.machines.items():
            host_keys = ["pc", "client"]
            image = machine.get_image()
            if "base" in image and any(key in name for key in host_keys):
                hosts.append(name)
        return hosts

    def get_base_hosts(self) -> list[Machine]:
        """
        Get the list of base hosts (all containers with Docker image kathara/base) in the lab.
        """
        hosts = []
        for name, machine in self.lab.machines.items():
            image = machine.get_image()
            if "base" in image:
                hosts.append(name)
        return hosts

    def get_host_net_config(self, host_name: str) -> dict:
        """
        Get the network configuration of a host, including ifconfig, ip addr, and ip route.
        """
        config = {}
        config["host_name"] = host_name
        config["ifconfig"] = self.exec_cmd(host_name, "ifconfig -a")
        config["ip_addr"] = self.exec_cmd(host_name, "ip addr")
        config["ip_route"] = self.exec_cmd(host_name, "ip route")
        return config

    def get_bmv2_switches(self) -> list[Machine]:
        """
        Get the list of bmv2 switches in the lab.
        """
        switches = []
        for name, machine in self.lab.machines.items():
            image = machine.get_image()
            if "p4" in image:
                switches.append(name)
        return switches

    def get_connected_devices(self, host_name: str) -> list[str]:
        """
        Get the list of devices connected to a host.
        """
        links: Dict[str:DockerLinkStats] = self._get_lab_link_stats()
        results = []
        for _, link in links.items():
            if link.name:
                if host_name == link.containers[0].labels["name"]:
                    results.append(link.containers[1].labels["name"])
                elif host_name == link.containers[1].labels["name"]:
                    results.append(link.containers[0].labels["name"])
        return results

    def get_default_gateway(self, host_name: str) -> str | None:
        """
        Get the default gateway of a host using `ip -j route`.
        """
        cmd = "ip -j route"
        result = self.exec_cmd(host_name, cmd)

        if isinstance(result, list):
            output = "\n".join(result)
        else:
            output = result

        try:
            routes = json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse `ip -j route` output: {e}") from e

        for r in routes:
            if r.get("dst") == "default":
                gw = r.get("gateway")
                if gw:
                    return gw

        return None

    def get_host_mac_address(self, host_name: str, iface: str = "eth0") -> str | None:
        """
        Get the MAC address of a host's interface.

        :param host_name: target host
        :param iface:     interface name, default "eth0"
        """
        cmd = f"cat /sys/class/net/{iface}/address"
        result = self.exec_cmd(host_name, cmd)
        if result:
            return result.strip()
        return None

    def get_host_ip(
        self, host_name: str, iface: str = "eth0", with_prefix: bool = False
    ) -> str | None:
        """
        Get the IPv4 address of a host via `ip -j addr`.
        Prefer the given interface (default: eth0).

        :param host_name: target host
        :param iface:     interface name, default "eth0"
        :param with_prefix: if True, return "ip/prefix" (e.g. 192.168.1.10/24)
                            if False, return only "ip" (e.g. 192.168.1.10)
        """

        cmd = "ip -j addr"
        # checked: a slow container right after deploy must retry, not hand a
        # "[TIMEOUT] ..." string to json.loads below.
        result = self.exec_cmd_checked(host_name, cmd)

        if isinstance(result, list):
            output = "\n".join(result)
        else:
            output = result

        try:
            ifaces = json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Failed to parse `ip -j addr` output from '{host_name}': {e}; "
                f"raw output started with: {output[:200]!r}"
            ) from e

        def format_ip(ip: str, prefix: Optional[int]) -> str:
            if with_prefix and prefix is not None:
                return f"{ip}/{prefix}"
            return ip

        for link in ifaces:
            if link.get("ifname") != iface:
                continue

            for addr in link.get("addr_info", []):
                if addr.get("family") != "inet":
                    continue
                ip = addr.get("local")
                prefix = addr.get("prefixlen")
                if ip and not ip.startswith("127."):
                    return format_ip(ip, prefix)

        for link in ifaces:
            for addr in link.get("addr_info", []):
                if addr.get("family") != "inet":
                    continue
                ip = addr.get("local")
                prefix = addr.get("prefixlen")
                if ip and not ip.startswith("127."):
                    return format_ip(ip, prefix)

        return None

    def get_host_interfaces(
        self, host_name: str, include_loopback: bool = False
    ) -> list[str]:
        cmd = "ip -j addr"
        result = self.exec_cmd(host_name, cmd)
        output = "\n".join(result) if isinstance(result, list) else result

        try:
            ifaces = json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse `ip -j addr` output: {e}") from e

        names = []
        for link in ifaces:
            name = link.get("ifname")
            if not name:
                continue
            if not include_loopback and name == "lo":
                continue
            if "br" in name:  # skip bridge interfaces
                continue
            names.append(name)

        return names

    def get_links(self) -> dict:
        """
        Get the links of the network.
        """
        links: Dict[str:DockerLinkStats] = self._get_lab_link_stats()
        result = {}
        for _, link in links.items():
            if link.name:
                result[link.name] = (
                    link.containers[0].labels["name"],
                    link.containers[1].labels["name"],
                )
        return result

    async def exec_cmd_async(self, host_name: str, command: str) -> str:
        """
        Run a command on a machine asynchronously and return its output as a string.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.exec_cmd, host_name, command)

    def _run_cmd(self, host_name: str, command: str, max_chars: int = 4000) -> str:
        """
        Run a command on a machine and return its output as a string,
        decoding bytes and filtering out None/empty/zeros.
        """
        try:
            output_generator = self.instance.exec(
                machine_name=host_name,
                command=command,
                lab_name=self.lab.name,
                stream=False,
            )
            for item in output_generator:
                if (
                    not item
                    or item == b""
                    or isinstance(item, int)
                    or item is None
                    or item == "None"
                ):
                    continue

                if isinstance(item, bytes):
                    out = item.decode("utf-8", errors="ignore").strip()
                elif isinstance(item, str):
                    out = item.strip()
                else:
                    out = str(item).strip()

                if len(out) > max_chars:
                    return (
                        out[:max_chars]
                        + f"...[truncated, {len(out) - max_chars} chars omitted]"
                    )

                return out

            return ""

        except MachineNotFoundError:
            return f"Machine {host_name} not found in lab {self.lab.name}."

        except Exception as e:
            return f"Error executing command on {host_name}: {e}"

    async def _check_ping_success_async(self, host: str, dst_ip: str) -> dict:
        PING_STATS_RE = re.compile(
            r"(?P<tx>\d+)\s+packets transmitted,\s+"
            r"(?P<rx>\d+)\s+(?:packets\s+)?received,\s+"
            r"(?P<loss>\d+(?:\.\d+)?)%\s+packet loss"
            r"(?:,\s*time\s*(?P<time>\d+)ms)?",
            re.MULTILINE,
        )

        RTT_RE = re.compile(
            r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
            r"([\d\.]+)/([\d\.]+)/([\d\.]+)/([\d\.]+)\s*ms",
            re.MULTILINE,
        )

        command = f"ping -c 2 -n -q {dst_ip}"
        result = await self.exec_cmd_async(host, command)

        stats_match = PING_STATS_RE.search(result)

        tx = rx = None
        loss = None
        time_ms = None
        rtt_min = rtt_avg = rtt_max = rtt_mdev = None

        if stats_match:
            tx = int(stats_match.group("tx"))
            rx = int(stats_match.group("rx"))
            loss = float(stats_match.group("loss"))
            if stats_match.group("time") is not None:
                time_ms = float(stats_match.group("time"))

        rtt_match = RTT_RE.search(result)
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

    async def get_reachability(self) -> str:
        return await self._get_reachability_async()

    def load_machines(self):
        self.bmv2_switches = []
        self.ovs_switches = []
        self.sdn_controllers = []
        self.hosts = []
        self.routers = []
        self.switches = []
        self.servers = defaultdict(list)

        machines: Dict[str, Machine] = self.lab.machines
        for machine, machine_obj in machines.items():
            image = machine_obj.get_image()
            if "p4" in image:
                self.bmv2_switches.append(machine)
            elif "frr" in image:
                self.routers.append(machine)
            elif "base" in image or "nginx" in image or "wireguard" in image:
                host_keys = ["pc", "client"]
                if any(key in machine for key in host_keys):
                    self.hosts.append(machine)
                elif "load_balancer" in machine or "lb" in machine:
                    self.servers["load_balancer"].append(machine)
                elif "switch" in machine or "sw" in machine:
                    self.switches.append(machine)
                elif "dns" in machine:
                    self.servers["dns"].append(machine)
                elif "dhcp" in machine:
                    self.servers["dhcp"].append(machine)
                elif "web" in machine and "backend" not in machine:
                    self.servers["web"].append(machine)
                elif "vpn" in machine:
                    self.servers["vpn"].append(machine)

            elif "influxdb" in image:
                self.servers["database"].append(machine)

            elif "sdn" in image:
                self.ovs_switches.append(machine)
            elif "pox" in image:
                self.sdn_controllers.append(machine)
            else:
                print(f"Unknown machine type: {machine} with image {image}")

        # sort all lists
        self.bmv2_switches = sorted(self.bmv2_switches)
        self.ovs_switches = sorted(self.ovs_switches)
        self.sdn_controllers = sorted(self.sdn_controllers)
        self.hosts = sorted(self.hosts)
        self.routers = sorted(self.routers)
        self.switches = sorted(self.switches)
        for server_type in self.servers:
            self.servers[server_type] = sorted(self.servers[server_type])

    async def _get_reachability_async(self) -> str:
        self.load_machines()

        host_names = list(self.hosts)
        for key, servers in self.servers.items():
            for server in servers:
                if server not in host_names:
                    host_names.append(server)

        host_ips = {host_name: self.get_host_ip(host_name) for host_name in host_names}

        coroutines = []
        pairs = []  # [(src, dst)]

        host_list = sorted(list(host_ips.items()))  # [(name, ip)]

        # if there is too many hosts, randomly sample 2 hosts as destinations
        if len(host_list) > 2:
            dst_list = host_list.copy()
            random.shuffle(dst_list)
            dst_list = dst_list[:2]
        else:
            dst_list = host_list

        for i in range(len(host_list)):
            src_name, src_ip = host_list[i]
            for j in range(len(dst_list)):
                dst_name, dst_ip = dst_list[j]
                if src_name == dst_name:
                    continue
                pairs.append((src_name, dst_name))
                coroutines.append(self._check_ping_success_async(src_name, dst_ip))

        responses = await asyncio.gather(*coroutines)

        results = []

        for (src, dst), stats in zip(pairs, responses):
            if stats is None or not isinstance(stats, dict):
                stats = {}

            result_entry = {
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

            results.append(result_entry)

        payload = {
            "hosts": host_ips,
            "results": results,
        }

        return json.dumps(payload, separators=(",", ":"))

    def ping_pair(
        self, host_a: str, host_b: str, count: int = 4, args: str = ""
    ) -> str:
        """
        Ping from one host to another in the lab.
        """
        ip_re = r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
        if not re.match(ip_re, host_b):
            host_b_ip = self.get_host_ip(host_b)
            if host_b_ip is None:
                return f"Cannot get IP address of host {host_b}."
            host_b = host_b_ip

        command = f"ping -c {count} {host_b} {args}"
        return self.exec_cmd(host_a, command)

    def traceroute(self, host_name: str, dst_ip: str) -> str:
        """
        Run a traceroute from a host to a destination IP.
        """
        command = f"traceroute {dst_ip}"
        return self.exec_cmd(host_name, command)

    def iperf_test(
        self,
        client_host_name: str,
        server_host_name: str,
        duration: int = 10,
        client_args: str = "",
        server_args: str = "",
    ) -> str:
        """
        Run an iperf test between two hosts.
        """
        # Start iperf server
        self.exec_cmd(server_host_name, f"iperf3 -s -D {server_args}")
        # Run iperf client
        result = self.exec_cmd(
            client_host_name,
            f"iperf3 -c {self.get_host_ip(server_host_name)} -t {duration} {client_args}",
        )
        # Stop iperf server
        self.exec_cmd(server_host_name, "pkill iperf3")
        return result

    def systemctl_ops(
        self,
        host_name: str,
        service_name: str,
        operation: Literal["start", "stop", "restart", "status"],
    ) -> str:
        """
        Perform systemctl operations (start, stop, restart, status) on a host.
        """
        result = self.exec_cmd(host_name, f"systemctl {operation} {service_name}")
        if operation != "status":
            time.sleep(5)
        return result

    def netstat(self, host_name: str, args: str = "-tuln") -> str:
        """
        Run netstat command on a host with given arguments.
        Example: -s for statistics, -tuln for listening ports.
        """
        command = f"netstat {args}"
        return self.exec_cmd(host_name, command)

    def ip_addr_statistics(self, host_name: str) -> str:
        """
        Get IP address statistics of a host.
        """
        command = "ip -s addr"
        return self.exec_cmd(host_name, command)

    def ethtool(self, host_name: str, interface: str, args: str = "") -> str:
        """
        Run ethtool command on a specific interface of a host with given arguments.
        """
        command = f"ethtool {interface} {args}"
        return self.exec_cmd(host_name, command)

    def curl_web_test(self, host_name: str, url: str, times: int = 5) -> str:
        """
        Perform a curl web test to a URL for several times and return timing statistics.
        """
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

    def ps(self, host_name: str, args: str = "aux") -> str:
        """
        Run ps command on a host with given arguments.
        Example: aux for all processes.
        """
        command = f"ps {args}"
        return self.exec_cmd(host_name, command)

    def show_dns_config(self, host_name: str) -> str:
        """
        Show DNS configuration of a host.
        """
        command = "cat /etc/resolv.conf"
        return self.exec_cmd(host_name, command)
