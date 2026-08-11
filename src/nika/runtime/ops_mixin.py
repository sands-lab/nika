"""Exec-based semantic operations mixed into LabRuntime backends.

Implementation lives in ``service/lab``; this module is a thin capability-aware
facade so orchestrator problems can keep calling ``runtime.<op>()``.
"""

from __future__ import annotations

from typing import Literal

from nika.service.lab.adapters import lab_api_for_runtime, node_status_from_container


class ExecSemanticOpsMixin:
    """Delegate semantic operations to ``service.lab`` APIs."""

    require_capabilities: object
    list_nodes: object

    def _api(self):
        return lab_api_for_runtime(self)

    def _delegate(self, capability: str, method: str, /, *args, **kwargs):
        self.require_capabilities(capability)
        return getattr(self._api(), method)(*args, **kwargs)

    def node_status(self, node: str) -> str:
        self.require_capabilities("node_status")
        try:
            return node_status_from_container(self.get_container(node))
        except ValueError:
            return "not_found"

    def set_interface_state(
        self, node: str, intf: str, state: Literal["up", "down"]
    ) -> str:
        return self._delegate("interface", "set_interface_state", node, intf, state)

    def get_interface_operstate(self, node: str, intf: str) -> str:
        return self._delegate("interface", "get_interface_operstate", node, intf)

    def get_host_ip(
        self, node: str, iface: str = "eth0", *, with_prefix: bool = False
    ) -> str | None:
        return self._delegate("ip", "get_host_ip", node, iface, with_prefix=with_prefix)

    def get_data_plane_host_ip(
        self, node: str, *, with_prefix: bool = False
    ) -> str | None:
        return self._delegate(
            "ip", "get_data_plane_host_ip", node, with_prefix=with_prefix
        )

    def get_default_gateway(self, node: str) -> str | None:
        return self._delegate("route", "get_default_gateway", node)

    def get_host_interfaces(
        self, node: str, *, include_loopback: bool = False
    ) -> list[str]:
        return self._delegate(
            "interface", "get_host_interfaces", node, include_loopback=include_loopback
        )

    def get_host_mac_address(self, node: str, iface: str = "eth0") -> str | None:
        return self._delegate("interface", "get_host_mac_address", node, iface)

    def list_nft_ruleset(self, node: str) -> str:
        return self._delegate("nft", "list_nft_ruleset", node)

    def add_nft_drop_rule(
        self,
        node: str,
        rule: str,
        *,
        table: str = "filter",
        family: str = "inet",
    ) -> None:
        self._delegate(
            "nft", "add_nft_drop_rule", node, rule, table=table, family=family
        )

    def delete_nft_table(
        self, node: str, *, table: str = "filter", family: str = "inet"
    ) -> None:
        self._delegate("nft", "delete_nft_table", node, table=table, family=family)

    def tc_set_netem(
        self,
        node: str | None = None,
        intf_name: str = "",
        *,
        host_name: str | None = None,
        loss: int | None = None,
        delay_ms: int | None = None,
        jitter_ms: int | None = None,
        duplicate: int | None = None,
        corrupt: int | None = None,
        reorder: int | None = None,
        limit: int | None = None,
        handle: str | None = None,
        parent: str | None = None,
    ) -> str:
        target_node = node if node is not None else host_name
        if target_node is None:
            raise TypeError("tc_set_netem requires node or host_name.")
        return self._delegate(
            "tc",
            "tc_set_netem",
            target_node,
            intf_name,
            loss=loss,
            delay_ms=delay_ms,
            jitter_ms=jitter_ms,
            duplicate=duplicate,
            corrupt=corrupt,
            reorder=reorder,
            limit=limit,
            handle=handle,
            parent=parent,
        )

    def tc_set_tbf(
        self,
        node: str | None = None,
        intf_name: str = "",
        *,
        host_name: str | None = None,
        rate: str,
        burst: str,
        limit: str,
        handle: str | None = None,
        parent: str | None = None,
    ) -> str:
        target_node = node if node is not None else host_name
        if target_node is None:
            raise TypeError("tc_set_tbf requires node or host_name.")
        return self._delegate(
            "tc",
            "tc_set_tbf",
            target_node,
            intf_name,
            rate=rate,
            burst=burst,
            limit=limit,
            handle=handle,
            parent=parent,
        )

    def tc_clear_intf(self, node: str, intf_name: str) -> str:
        return self._delegate("tc", "tc_clear_intf", node, intf_name)

    def tc_show_intf(self, node: str, intf_name: str) -> str:
        return self._delegate("tc", "tc_show_intf", node, intf_name)

    def systemctl(
        self, node: str, service: str, operation: Literal["start", "stop", "restart"]
    ) -> str:
        return self._delegate("service", "systemctl", node, service, operation)

    def frr_get_bgp_asn_number(self, node: str) -> int:
        return self._delegate("frr", "frr_get_bgp_asn_number", node)

    def uses_srl_router(self, node: str) -> bool:
        return self._api().uses_srl_router(node)

    def srl_get_bgp_as(self, node: str) -> int:
        return self._api().srl_get_bgp_as(node)

    def srl_set_bgp_as(self, node: str, asn: int) -> None:
        self._api().srl_set_bgp_as(node, asn)

    def srl_add_bgp_acl_drop_179(self, node: str) -> None:
        self._api().srl_add_bgp_acl_drop_179(node)

    def srl_bgp_acl_drop_179_present(self, node: str) -> bool:
        return self._api().srl_bgp_acl_drop_179_present(node)

    def srl_withdraw_client_prefix(
        self, node: str, *, subinterface: str = "ethernet-1/2.0"
    ) -> None:
        self._api().srl_withdraw_client_prefix(node, subinterface=subinterface)

    def srl_client_subinterface_disabled(self, node: str, *, subinterface: str) -> bool:
        return self._api().srl_client_subinterface_disabled(
            node, subinterface=subinterface
        )

    def srl_add_blackhole_static(self, node: str, prefix: str) -> None:
        self._api().srl_add_blackhole_static(node, prefix)

    def srl_blackhole_static_present(self, node: str, prefix: str) -> bool:
        return self._api().srl_blackhole_static_present(node, prefix)

    def srl_advertise_prefix(self, node: str, prefix: str) -> None:
        self._api().srl_advertise_prefix(node, prefix)

    def srl_prefix_advertised(self, node: str, prefix: str) -> bool:
        return self._api().srl_prefix_advertised(node, prefix)

    def srl_add_blackhole_route_leak(self, node: str, prefix: str) -> None:
        self._api().srl_add_blackhole_route_leak(node, prefix)

    def srl_withdraw_bgp_prefix(self, node: str, prefix: str) -> None:
        self._api().srl_withdraw_bgp_prefix(node, prefix)

    def srl_bgp_prefix_withdrawn(self, node: str, prefix: str) -> bool:
        return self._api().srl_bgp_prefix_withdrawn(node, prefix)

    def kill_process(self, node: str, process_name: str) -> str:
        return self._delegate("process", "kill_process", node, process_name)

    def write_file(self, node: str, path: str, content: str) -> str:
        return self._delegate("file", "write_file", node, path, content)

    def renew_dhcp_leases(self, nodes: list[str], intf: str = "eth0") -> None:
        self.require_capabilities("dns")
        self._api().renew_dhcp_leases(nodes, intf)

    def dhcp_set_option_routers(
        self, dhcp_server: str, subnet: str, gateway: str
    ) -> None:
        self._delegate("dns", "dhcp_set_option_routers", dhcp_server, subnet, gateway)

    def dhcp_set_option_dns(self, dhcp_server: str, subnet: str, dns: str) -> None:
        self._delegate("dns", "dhcp_set_option_dns", dhcp_server, subnet, dns)

    def dhcp_delete_subnet(self, dhcp_server: str, subnet: str) -> None:
        self._delegate("dns", "dhcp_delete_subnet", dhcp_server, subnet)

    def list_dhcp_client_nodes(self) -> list[str]:
        self.require_capabilities("dns")
        return [
            node
            for node in self.list_nodes()
            if any(key in node for key in ("pc", "client"))
        ]

    def process_running(self, node: str, process_name: str) -> bool:
        return self._delegate("process", "process_running", node, process_name)

    def process_not_running(self, node: str, process_name: str) -> bool:
        return self._delegate("process", "process_not_running", node, process_name)

    def pidfile_running(self, node: str, pidfile: str) -> bool:
        return self._delegate("pidfile", "pidfile_running", node, pidfile)

    def interface_exists(self, node: str, intf: str) -> bool:
        return self._delegate("interface", "interface_exists", node, intf)

    def tc_qdisc_contains(self, node: str, intf: str, keyword: str) -> bool:
        return self._delegate("tc", "tc_qdisc_contains", node, intf, keyword)

    def iptables_rule_present(self, node: str, chain: str, rule_args: str) -> bool:
        return self._delegate(
            "iptables", "iptables_rule_present", node, chain, rule_args
        )

    def nft_ruleset_contains(self, node: str, pattern: str) -> bool:
        return self._delegate("nft", "nft_ruleset_contains", node, pattern)

    def ping_ok(self, node: str, target: str, *, count: int = 1) -> bool:
        return self._delegate("exec", "ping_ok", node, target, count=count)

    def dig_query(
        self, node: str, domain: str, *, nameserver: str | None = None
    ) -> str:
        return self._delegate("dns", "dig_query", node, domain, nameserver=nameserver)

    def file_contains(self, node: str, path: str, pattern: str) -> bool:
        return self._delegate("file", "file_contains", node, path, pattern)

    def start_background_od_traffic(
        self,
        od_dicts: dict[str, dict[str, int]],
        *,
        interval: int = 5,
        unit: str = "M",
        udp: bool = True,
        server_args: str = "",
        client_args: str = "",
        host_ips: dict[str, str] | None = None,
    ) -> list[str]:
        self.require_capabilities("traffic", "ip")
        return self._api().start_background_od_traffic(
            od_dicts,
            interval=interval,
            unit=unit,
            udp=udp,
            server_args=server_args,
            client_args=client_args,
            host_ips=host_ips,
        )
