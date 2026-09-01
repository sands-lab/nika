"""Backend-neutral semantic lab operations shared by Kathara and Containerlab."""

from __future__ import annotations

import base64
import json
import re
import shlex
from typing import Literal

from nika.service.lab.protocols import SupportsExec


class SemanticOpsMixin:
    """Host, network, process, and service operations via ``exec_cmd``."""

    def get_interface_operstate(self: SupportsExec, node: str, intf: str) -> str:
        quoted = shlex.quote(intf)
        output = self.exec_cmd(node, f"cat /sys/class/net/{quoted}/operstate")
        return output.strip().lower()

    def set_interface_state(
        self: SupportsExec, node: str, intf: str, state: Literal["up", "down"]
    ) -> str:
        quoted = shlex.quote(intf)
        return self.exec_cmd(node, f"ip link set {quoted} {state}")

    def get_host_ip(
        self: SupportsExec,
        node: str,
        iface: str = "eth0",
        *,
        with_prefix: bool = False,
    ) -> str | None:
        output = self.exec_cmd(node, "ip -j addr")
        try:
            ifaces = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse `ip -j addr` output on {node}: {exc}"
            ) from exc

        def format_ip(ip: str, prefix: int | None) -> str:
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

    def get_data_plane_host_ip(
        self: SupportsExec,
        node: str,
        *,
        with_prefix: bool = False,
        prefer_ifaces: tuple[str, ...] = ("eth1", "eth0"),
    ) -> str | None:
        """Prefer data-plane ifaces (eth1 on Containerlab linux) over mgmt eth0."""
        candidates: list[str] = []
        for iface in prefer_ifaces:
            ip = self.get_host_ip(node, iface, with_prefix=with_prefix)
            if not ip:
                continue
            bare = ip.split("/", 1)[0]
            # Containerlab mgmt bridge ranges used by NIKA labs.
            if bare.startswith(("172.100.", "172.20.", "172.18.")):
                candidates.append(ip)
                continue
            return ip
        return candidates[0] if candidates else None

    def get_default_gateway(self: SupportsExec, node: str) -> str | None:
        output = self.exec_cmd(node, "ip -j route")
        try:
            routes = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse `ip -j route` output on {node}: {exc}"
            ) from exc
        for route in routes:
            if route.get("dst") == "default":
                gateway = route.get("gateway")
                if gateway:
                    return gateway
        return None

    def get_host_interfaces(
        self: SupportsExec, node: str, *, include_loopback: bool = False
    ) -> list[str]:
        output = self.exec_cmd(node, "ip -j addr")
        try:
            ifaces = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse `ip -j addr` output on {node}: {exc}"
            ) from exc
        names: list[str] = []
        for link in ifaces:
            name = link.get("ifname")
            if not name:
                continue
            if not include_loopback and name == "lo":
                continue
            if "br" in name:
                continue
            names.append(name)
        return names

    def get_host_mac_address(
        self: SupportsExec, node: str, iface: str = "eth0"
    ) -> str | None:
        quoted = shlex.quote(iface)
        result = self.exec_cmd(node, f"cat /sys/class/net/{quoted}/address").strip()
        return result or None

    def systemctl(
        self: SupportsExec,
        node: str,
        service: str,
        operation: Literal["start", "stop", "restart"],
    ) -> str:
        return self.exec_cmd(node, f"systemctl {operation} {service}")

    def kill_process(self: SupportsExec, node: str, process_name: str) -> str:
        return self.exec_cmd(node, f"pkill -9 {process_name} 2>/dev/null; true")

    def write_file(self: SupportsExec, node: str, path: str, content: str) -> str:
        encoded = base64.b64encode(content.encode()).decode()
        quoted_path = shlex.quote(path)
        return self.exec_cmd(node, f"echo {encoded} | base64 -d > {quoted_path}")

    def renew_dhcp_leases(
        self: SupportsExec, nodes: list[str], intf: str = "eth0"
    ) -> None:
        quoted_intf = shlex.quote(intf)
        for node in nodes:
            self.exec_cmd(node, f"dhclient -r {quoted_intf}")
            self.exec_cmd(node, f"dhclient -v {quoted_intf}")

    @staticmethod
    def _subnet_escaped(subnet: str) -> str:
        return subnet.replace(".", "\\.")

    def dhcp_set_option_routers(
        self: SupportsExec, dhcp_server: str, subnet: str, gateway: str
    ) -> None:
        sub = self._subnet_escaped(subnet)
        cmd = (
            f"sed -i '/subnet {sub} netmask 255\\.255\\.255\\.0/,/}}/ "
            f"s/option routers .*/option routers {gateway};/' /etc/dhcp/dhcpd.conf"
        )
        self.exec_cmd(dhcp_server, cmd)
        self.systemctl(dhcp_server, "isc-dhcp-server", "restart")

    def dhcp_set_option_dns(
        self: SupportsExec, dhcp_server: str, subnet: str, dns: str
    ) -> None:
        sub = self._subnet_escaped(subnet)
        cmd = (
            f"sed -i '/subnet {sub} netmask 255\\.255\\.255\\.0/,/}}/ "
            f"s/option domain-name-servers .*/option domain-name-servers {dns};/' /etc/dhcp/dhcpd.conf"
        )
        self.exec_cmd(dhcp_server, cmd)
        self.systemctl(dhcp_server, "isc-dhcp-server", "restart")

    def dhcp_delete_subnet(self: SupportsExec, dhcp_server: str, subnet: str) -> None:
        self.exec_cmd(dhcp_server, "cp /etc/dhcp/dhcpd.conf /etc/dhcp/dhcpd.conf.bak")
        escaped_sub = re.escape(subnet)
        script = (
            "import re\n"
            "from pathlib import Path\n"
            f"sub = {subnet!r}\n"
            "path = Path('/etc/dhcp/dhcpd.conf')\n"
            "text = path.read_text()\n"
            f"pattern = re.compile(r'^\\s*subnet\\s+{escaped_sub}\\s+netmask\\s', re.M)\n"
            "if not pattern.search(text):\n"
            "    raise SystemExit(f'subnet block not found for {sub!r}')\n"
            "out = []\n"
            "skip = False\n"
            "depth = 0\n"
            "for line in text.splitlines(True):\n"
            "    if (not skip) and pattern.match(line):\n"
            "        skip = True\n"
            "        depth = line.count('{') - line.count('}')\n"
            "        continue\n"
            "    if skip:\n"
            "        depth += line.count('{') - line.count('}')\n"
            "        if depth <= 0:\n"
            "            skip = False\n"
            "        continue\n"
            "    out.append(line)\n"
            "path.write_text(''.join(out))\n"
        )
        self.write_file(dhcp_server, "/tmp/nika_dhcp_delete_subnet.py", script)
        self.exec_cmd(dhcp_server, "python3 /tmp/nika_dhcp_delete_subnet.py")
        self.systemctl(dhcp_server, "isc-dhcp-server", "restart")
        self.exec_cmd(
            dhcp_server,
            "for i in 1 2 3 4 5; do "
            "systemctl is-active --quiet isc-dhcp-server && break; sleep 1; done",
        )

    def process_running(self: SupportsExec, node: str, process_name: str) -> bool:
        quoted = shlex.quote(process_name)
        output = self.exec_cmd(
            node, f"pgrep -a {quoted} 2>/dev/null || echo NONE"
        ).strip()
        return output != "NONE" and process_name in output

    def process_not_running(self: SupportsExec, node: str, process_name: str) -> bool:
        return not self.process_running(node, process_name)

    def pidfile_running(self: SupportsExec, node: str, pidfile: str) -> bool:
        quoted = shlex.quote(pidfile)
        output = self.exec_cmd(
            node,
            f"if [ -f {quoted} ] && kill -0 $(cat {quoted}) 2>/dev/null; then echo running; else echo not_running; fi",
        )
        return output.strip() == "running"

    def interface_exists(self: SupportsExec, node: str, intf: str) -> bool:
        quoted = shlex.quote(intf)
        output = self.exec_cmd(node, f"ip link show {quoted} 2>&1")
        lowered = output.lower()
        return "does not exist" not in lowered and "no such device" not in lowered

    def iptables_rule_present(
        self: SupportsExec, node: str, chain: str, rule_args: str
    ) -> bool:
        check_cmd = f"iptables -C {chain} {rule_args} >/dev/null 2>&1 && echo present || echo absent"
        return self.exec_cmd(node, check_cmd).strip() == "present"

    def ping_ok(self: SupportsExec, node: str, target: str, *, count: int = 1) -> bool:
        quoted = shlex.quote(target)
        output = self.exec_cmd(node, f"ping -c {count} -W 2 {quoted} 2>&1")
        return " 0% packet loss" in output or " 0% loss" in output

    def dig_query(
        self: SupportsExec, node: str, domain: str, *, nameserver: str | None = None
    ) -> str:
        ns = f"@{shlex.quote(nameserver)} " if nameserver else ""
        quoted_domain = shlex.quote(domain)
        return self.exec_cmd(
            node, f"dig +short {ns}{quoted_domain} 2>/dev/null"
        ).strip()

    def file_contains(self: SupportsExec, node: str, path: str, pattern: str) -> bool:
        quoted_path = shlex.quote(path)
        quoted_pat = shlex.quote(pattern)
        output = self.exec_cmd(
            node,
            f"grep -q -F {quoted_pat} {quoted_path} 2>/dev/null && echo yes || echo no",
        )
        return output.strip() == "yes"

    def start_background_od_traffic(
        self: SupportsExec,
        od_dicts: dict[str, dict[str, int]],
        *,
        interval: int = 5,
        unit: str = "M",
        udp: bool = True,
        server_args: str = "",
        client_args: str = "",
        host_ips: dict[str, str] | None = None,
    ) -> list[str]:
        started_server_ports: dict[str, int] = {}
        server_port_assign: dict[str, dict[str, int]] = {}
        labels: list[str] = []
        start_port_id = 5201
        ip_overrides = dict(host_ips or {})

        for src_host, dests in od_dicts.items():
            for dst_host in dests:
                if src_host == dst_host:
                    continue
                labels.append(f"{src_host}_to_{dst_host}")
                if dst_host in started_server_ports:
                    started_server_ports[dst_host] += 1
                    dst_port = started_server_ports[dst_host]
                    server_port_assign[dst_host][src_host] = dst_port
                else:
                    dst_port = start_port_id
                    started_server_ports[dst_host] = start_port_id
                    server_port_assign[dst_host] = {src_host: start_port_id}
                server_cmd = f"iperf3 -s -p {dst_port} {server_args} -J &"
                self.exec_cmd(dst_host, server_cmd)

        for src_host, dests in od_dicts.items():
            for dst_host, volume in dests.items():
                if src_host == dst_host:
                    continue
                dst_ip = ip_overrides.get(dst_host) or self.get_data_plane_host_ip(
                    dst_host
                )
                if not dst_ip:
                    raise ValueError(f"Cannot resolve IP for host {dst_host!r}")
                dst_port = server_port_assign[dst_host][src_host]
                client_udp = " -u" if udp else ""
                client_cmd = (
                    f"iperf3 -c {dst_ip} -p {dst_port} -b {volume}{unit} "
                    f"-t {interval}{client_udp} {client_args} -l 1472 -J &"
                )
                self.exec_cmd(src_host, client_cmd)

        return labels
