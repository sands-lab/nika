"""Campus LAN fabric with DHCP, DNS, and load-balanced HTTP services."""

from __future__ import annotations

import re
from collections import deque
from ipaddress import IPv4Interface
from typing import Literal

from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.native_routers import NativeRouterEnv

_ADDRESS = re.compile(r"ip addr add (\S+) dev (\S+)")


class CampusLan(NativeRouterEnv, NetworkEnvBase):
    ROUTING_PARAMETER_KINDS = frozenset({"ospf_costs"})
    LAB_NAME = "campus_lan"
    # Deploys the netlab configurations stored under rendered/.
    STORES_RENDERED_CONFIGS = True
    TOPO_LEVEL = "medium"
    # Distribution and server gateways bridge access ports and relay DHCP;
    # XRd Control Plane cannot route a bridged interface, so they stay FRR.
    # RouterOS routes bridged interfaces and relays DHCP, so it also runs them.
    NATIVE_PROFILE_ROLES = {
        "iosxr": frozenset({"core"}),
        "routeros": frozenset({"core", "distribution", "server_gateway"}),
    }
    NATIVE_PROFILE_SIZES = {"routeros": ("s",)}

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        routing_parameters: dict | None = None,
        device_profile: str | None = None,
        rendered: bool = True,
        **kwargs,
    ):
        from nika.net_env.campus_lan.lab_dhcp import (
            CampusLanDhcp,
        )

        donor = CampusLanDhcp(topo_size=topo_size, **kwargs)

        self.__dict__.update(donor.__dict__)
        self.topo_size = topo_size
        from nika.net_env.synthesis.parameters import apply_routing_parameters

        apply_routing_parameters(self, routing_parameters)

        self.name = self.LAB_NAME
        if getattr(self, "lab", None) is not None:
            self.lab.name = self.LAB_NAME
        self.apply_device_profile(device_profile)
        if rendered and routing_parameters is None:
            from nika.net_env.synthesis.rendered import use_rendered_configs

            use_rendered_configs(self)

    def router_roles(self) -> dict[str, list[str]]:
        roles = {"core": [], "distribution": [], "server_gateway": []}
        for name in self.routers:
            if name.startswith("router_core_"):
                roles["core"].append(name)
            elif name.startswith("router_dist_"):
                roles["distribution"].append(name)
            else:
                roles["server_gateway"].append(name)
        return roles

    def _router_queries(self):
        from nika.net_env.campus_lan.verify import CORE_ROUTER, FRR_QUERIES

        profile = self.native_routers.get(CORE_ROUTER)
        if profile == "routeros":
            from nika.net_env.campus_lan.verify import DIST_ROUTER_DHCP
            from nika.net_env.utils.routeros.platform import routeros_campus_queries

            return routeros_campus_queries(
                FRR_QUERIES,
                gateways=self.native_routers.get(DIST_ROUTER_DHCP) == "routeros",
            )
        if profile != "iosxr":
            return FRR_QUERIES
        from dataclasses import replace

        from nika.net_env.utils.iosxr import platform

        return replace(
            FRR_QUERIES,
            ospf_process=platform.IOSXR_OSPF_PROCESS,
            ospf_running=platform.iosxr_ospf_running,
            ospf_neighbors=platform.IOSXR_OSPF_NEIGHBORS,
            ospf_full_count=platform.iosxr_ospf_full_count,
        )

    def target_roles(self) -> dict[str, list[str]]:
        hosts = list(self.hosts or [])
        pcs = [h for h in hosts if h.startswith("pc_")] or hosts
        return {
            **super().target_roles(),
            "hosts": pcs,
            "host1_pool": pcs,
            "web": pcs,
            "attacker_pool": pcs,
        }

    def business_workload(self):
        from nika.net_env.campus_lan.workload import build_workload

        return build_workload(
            list(self.hosts or []), self.frr_routers(), self.router_rate_limit_kbps()
        )

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="pc_1_1_1_1",
            dst_ip="10.200.0.3",
            http_url="http://web0.local/",
            http_name_url="http://web0.local/",
            control_plane_host="router_dist_1_1",
            peer_host="pc_2_1_1_1",
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab_startup

        return verify_campus_lan_lab_startup(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            queries=self._router_queries(),
        )

    def verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab

        return verify_campus_lan_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            queries=self._router_queries(),
        )

    def link_symptom_targets(self, node: str, intf: str) -> dict[str, str] | None:
        """Observer and destination whose path crosses the link at ``node:intf``.

        A server link is probed at the server's own address; a link on a PC's
        shortest paths to the web server from that PC; any other
        router-to-router link at the far router's address on it, from the PC
        nearer the other router.
        """
        match = re.fullmatch(r"eth(\d+)", intf)
        machine = self.lab.machines.get(node)
        iface = machine.interfaces.get(int(match[1])) if machine and match else None
        if iface is None:
            return None
        ends = set(iface.link.machines)
        graph = self._machine_graph()
        addresses = self._startup_addresses()
        path = self.default_probe_path()
        target = next(
            (
                name
                for name, by_intf in addresses.items()
                if any(str(a.ip) == path.dst_ip for a in by_intf.values())
            ),
            None,
        )
        pcs = sorted(h for h in self.hosts or () if h.startswith("pc_"))
        if target is None or not pcs:
            return None

        def hops(src: str, cut: set[str] | None = None) -> dict[str, int]:
            seen, queue = {src: 0}, deque([src])
            while queue:
                here = queue.popleft()
                for there in graph[here]:
                    if there in seen or (cut and {here, there} <= cut):
                        continue
                    seen[there] = seen[here] + 1
                    queue.append(there)
            return seen

        def crosses(src: str) -> bool:
            """Every shortest path from ``src`` to the target uses the link."""
            detour = hops(src, ends).get(target)
            return detour is None or detour > hops(src)[target]

        if crosses(path.src_host):
            return None
        routers = set(self.routers or ())
        hosts = [end for end in sorted(ends) if end not in routers and end not in pcs]
        # Probe the faulted host's own address, or the host across the port.
        if node in hosts and intf in addresses.get(node, {}):
            server, address = node, addresses[node][intf]
        else:
            server = next((h for h in hosts if addresses.get(h)), None)
            address = next(iter(addresses[server].values())) if server else None
        if server and address:
            # A host behind another host (a load balancer's backend) is reached
            # from its neighbor there, its gateway first. The others are reached
            # from the default web server: a statically addressed observer, as
            # a DHCP client loses its address while the DHCP server's link is
            # down.
            others = sorted(
                (h for h in hosts if h != server),
                key=lambda h: len(addresses.get(h, {})) < 2,
            )
            observer = others[0] if others else target
            if observer == server:
                observer = path.src_host
            return {"observer_device": observer, "probe_dst_ip": str(address.ip)}
        for pc in pcs:
            if crosses(pc):
                return {"observer_device": pc, "probe_dst_ip": path.dst_ip}
        if len(ends) != 2 or not ends <= routers:
            return None
        for near, far in (sorted(ends), sorted(ends)[::-1]):
            address = next(
                (
                    a
                    for name, a in addresses.get(far, {}).items()
                    if self._link_of(far, name) == iface.link
                ),
                None,
            )
            if address is None:
                continue
            for pc in pcs:
                distance = hops(pc)
                if distance.get(near, 1 << 30) < distance.get(far, 1 << 30):
                    return {"observer_device": pc, "probe_dst_ip": str(address.ip)}
        return None

    def _machine_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {name: set() for name in self.lab.machines}
        for machine in self.lab.machines.values():
            for iface in machine.interfaces.values():
                graph[machine.name] |= set(iface.link.machines) - {machine.name}
        return graph

    def _link_of(self, node: str, intf: str):
        match = re.fullmatch(r"eth(\d+)", intf)
        iface = self.lab.machines[node].interfaces.get(int(match[1])) if match else None
        return iface.link if iface is not None else None

    def _startup_addresses(self) -> dict[str, dict[str, IPv4Interface]]:
        """Interface addresses from each machine's startup, before conversion."""
        sources = dict(getattr(self, "native_sources", {}) or {})
        found: dict[str, dict[str, IPv4Interface]] = {}
        for name in self.lab.machines:
            path = f"{name}.startup"
            if name in sources:
                text = sources[name][1]
            elif self.lab.fs.exists(path):
                text = self.lab.fs.readtext(path)
            else:
                continue
            found[name] = {
                dev: IPv4Interface(cidr) for cidr, dev in _ADDRESS.findall(text)
            }
        return found
