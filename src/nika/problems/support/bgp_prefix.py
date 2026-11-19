"""Shared operations for BGP prefix limits and excess advertisements."""

from __future__ import annotations

import ipaddress
import json
import random
import re

from nika.net_env.isp.identity import list_isp_base_scenarios
from nika.problems.support.frr import FRR_SAVE_COMMAND, frr_config_command
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError

_FLOOD_PREFIX_BASE = "198.19"
_FLOOD_PREFIX_LIST = "TE-MORE-SPECIFICS"


class BGPPrefixOperations:
    def _frr(self, router: str, command: str) -> str:
        """Run ``command`` beside ``router``'s FRR: on the node, or through its platform."""
        ops = native_fault_ops(self, router, "frr_shell", frr_fallback=True)
        if ops is not None:
            return ops.frr_shell(command)
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: {router!r} does not route with FRR."
            )
        return self.runtime.exec(router, command)

    def _frr_save(self, router: str) -> None:
        self._frr(router, FRR_SAVE_COMMAND)

    def _bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        return bgp if isinstance(bgp, dict) else {}

    def _resolve_session(self, params) -> tuple[str, str, str]:
        receiver = params.receiver_name
        peer = params.peer_name
        neighbor_ip = params.neighbor_ip
        if neighbor_ip:
            return receiver, peer, neighbor_ip

        bgp = self._bgp_inventory()
        sessions = [
            s
            for s in (bgp.get("sessions") or [])
            if str(s.get("session_type") or "") == "ebgp"
            and str(s.get("local_device") or "") == receiver
            and str(s.get("remote_device") or "") == peer
            and s.get("remote_ip")
        ]
        if sessions:
            sessions = sorted(sessions, key=lambda s: str(s.get("remote_ip") or ""))
            return receiver, peer, str(sessions[0]["remote_ip"])

        # Fall back to any eBGP session matching the device pair (either direction).
        for s in bgp.get("sessions") or []:
            if str(s.get("session_type") or "") != "ebgp":
                continue
            local = str(s.get("local_device") or "")
            remote = str(s.get("remote_device") or "")
            if {local, remote} == {receiver, peer} and s.get("remote_ip"):
                if local == receiver:
                    return receiver, peer, str(s["remote_ip"])
                if s.get("local_ip"):
                    return receiver, peer, str(s["local_ip"])

        raise RuntimeCapabilityError(
            f"{type(self).__name__} could not resolve eBGP neighbor IP for "
            f"receiver={receiver!r} peer={peer!r}; pass neighbor_ip or deploy "
            "isp with bgp_mode=ebgp."
        )

    def _neighbor_output(self, router: str, neighbor_ip: str) -> str:
        output = self._frr(
            router,
            f"vtysh -c 'show bgp neighbors {neighbor_ip}' 2>/dev/null || true",
        )
        if f"BGP neighbor is {neighbor_ip}," not in output:
            raise ValueError(f"Unreadable BGP neighbor {neighbor_ip} on {router!r}")
        return output

    def _session_established(self, router: str, neighbor_ip: str) -> bool:
        ops = native_fault_ops(self, router, "bgp_established_peers", frr_fallback=True)
        if ops is not None:
            return neighbor_ip in ops.bgp_established_peers()
        out = self._neighbor_output(router, neighbor_ip)
        for line in out.splitlines():
            lower = line.lower()
            if "bgp state" in lower and "established" in lower:
                return True
        return False

    def _received_prefix_count(self, router: str, neighbor_ip: str) -> int:
        ops = native_fault_ops(
            self, router, "bgp_received_prefix_count", frr_fallback=True
        )
        if ops is not None:
            return ops.bgp_received_prefix_count(neighbor_ip)
        out = self._neighbor_output(router, neighbor_ip)
        for pattern in (
            r"Prefix received(?: count)?:\s*(\d+)",
            r"Accepted prefixes?:\s*(\d+)",
            r"prefixes received[:\s]+(\d+)",
        ):
            match = re.search(pattern, out, re.IGNORECASE)
            if match:
                return int(match.group(1))
        summary = self._frr(
            router, "vtysh -c 'show bgp summary json' 2>/dev/null || true"
        )
        peers = json.loads(summary)["ipv4Unicast"]["peers"]
        return int(peers[neighbor_ip]["pfxRcd"])

    def customer_route(self, receiver: str, peer: str) -> dict | None:
        """The scenario's customer route that ``receiver`` learns only from ``peer``."""
        site = self._bgp_inventory().get("customer_site")
        if (
            isinstance(site, dict)
            and site.get("partner") == receiver
            and site.get("provider") == peer
        ):
            return site
        return None

    def _has_route(self, router: str, prefix: str) -> bool:
        ops = native_fault_ops(self, router, "has_bgp_route", frr_fallback=True)
        if ops is not None:
            return ops.has_bgp_route(prefix)
        out = self._frr(
            router, f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true"
        )
        if "Network not in table" in out:
            return False
        paths = re.search(r"Paths:\s*\((\d+) available", out)
        if f"BGP routing table entry for {prefix}" not in out or paths is None:
            raise ValueError(f"Unreadable BGP route {prefix} on {router!r}")
        return int(paths[1]) > 0

    def _bgp_routers(self, receiver: str, peer: str) -> list[str]:
        """Every router with a BGP session in the inventory."""
        routers = {
            str(s.get("local_device") or "")
            for s in self._bgp_inventory().get("sessions") or []
        }
        return sorted({receiver, peer, *routers} - {""})

    def _flood_policy_maps(self, device: str) -> list[str]:
        maps: list[str] = []

        def read() -> bool:
            # vtysh can return an empty configuration while bgpd resets
            # sessions, such as right after another failure changed a limit.
            config = self._frr(device, "vtysh -c 'show running-config'")
            maps[:] = sorted(
                set(
                    re.findall(
                        r"^\s*neighbor \S+ route-map ([A-Za-z0-9_.-]+) (?:in|out)$",
                        config,
                        re.M,
                    )
                )
            )
            return bool(maps)

        wait_until(read, 30, interval_s=3)
        if not maps:
            raise RuntimeCapabilityError(
                "BGP flood setup requires bound import/export policies"
            )
        return maps

    def _install_flood_policy(self, device: str) -> None:
        ops = native_fault_ops(self, device, "permit_bgp_prefixes", frr_fallback=True)
        if ops is not None:
            ops.permit_bgp_prefixes("198.19.0.0/16", 24)
            return
        # Include solved import policies, not only the baseline policy names.
        lines = [
            f"ip prefix-list {_FLOOD_PREFIX_LIST} seq 5 permit 198.19.0.0/16 le 24"
        ]
        for policy in self._flood_policy_maps(device):
            lines += [
                f"route-map {policy} permit 1",
                f"match ip address prefix-list {_FLOOD_PREFIX_LIST}",
            ]
        self._frr(device, frr_config_command(*lines))
        self._frr_save(device)

    def _flood_prefixes(self, count: int, *, peer: str, neighbor_ip: str) -> list[str]:
        """Pick ``count`` /24s scattered within the flood /16, not a tidy block.

        Real de-aggregation/leak incidents (e.g. Optus 2023) announce many
        non-contiguous more-specifics, not one ascending sequential range.
        The selection is deterministic per session (seeded from the peer and
        neighbor address) so repeated runs of the same case see the same
        flood set.
        """
        rng = random.Random(f"{peer}|{neighbor_ip}")
        octets = list(range(256))
        rng.shuffle(octets)
        count = min(count, 256)
        return sorted(f"{_FLOOD_PREFIX_BASE}.{octet}.0/24" for octet in octets[:count])

    def _advertise_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        ops = native_fault_ops(self, peer, "advertise_bgp_prefixes", frr_fallback=True)
        if ops is not None:
            ops.advertise_bgp_prefixes(prefixes)
            return
        lines: list[str] = []
        config = self._frr(peer, "vtysh -c 'show running-config'")
        if "no bgp network import-check" not in config:
            # FRR originates a ``network`` only when a matching route exists,
            # so back the announcements with discard routes under import check.
            lines.extend(f"ip route {prefix} Null0" for prefix in prefixes)
        lines += [f"router bgp {asn}", "address-family ipv4 unicast"]
        lines.extend(f"network {prefix}" for prefix in prefixes)
        self._frr(peer, frr_config_command(*lines))
        self._frr_save(peer)
        self._frr(peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true")

    def _withdraw_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        lines = [f"router bgp {asn}", "address-family ipv4 unicast"]
        lines.extend(f"no network {prefix}" for prefix in prefixes)
        config = self._frr(peer, "vtysh -c 'show running-config'")
        statics = [p for p in prefixes if f"ip route {p} Null0" in config]
        if statics:
            lines += ["exit-address-family", "exit"]
            lines.extend(f"no ip route {prefix} Null0" for prefix in statics)
        self._frr(peer, frr_config_command(*lines))
        self._frr_save(peer)

    def _set_maximum_prefix(
        self, receiver: str, asn: int, neighbor_ip: str, limit: int
    ) -> None:
        ops = native_fault_ops(
            self, receiver, "set_bgp_prefix_limit", frr_fallback=True
        )
        if ops is not None:
            ops.set_bgp_prefix_limit(neighbor_ip, limit)
            return
        cmd = frr_config_command(
            f"router bgp {asn}",
            "address-family ipv4 unicast",
            f"neighbor {neighbor_ip} maximum-prefix {limit}",
        )
        self._frr(receiver, cmd)
        self._frr_save(receiver)

    def _clear_maximum_prefix(self, receiver: str, asn: int, neighbor_ip: str) -> None:
        cmd = frr_config_command(
            f"router bgp {asn}",
            "address-family ipv4 unicast",
            f"no neighbor {neighbor_ip} maximum-prefix",
        )
        self._frr(receiver, cmd)
        self._frr_save(receiver)

    def _wait_session_state(
        self,
        router: str,
        neighbor_ip: str,
        *,
        established: bool,
        timeout_s: float = 90.0,
        poll_s: float = 2.0,
    ) -> bool:
        return wait_until(
            lambda: self._session_established(router, neighbor_ip) == established,
            timeout_s,
            interval_s=poll_s,
        )

    def _discover_flood_prefixes(self, peer: str) -> list[str]:
        ops = native_fault_ops(self, peer, "bgp_networks", frr_fallback=True)
        if ops is not None:
            return sorted(
                p
                for p in ops.bgp_networks()
                if (network := ipaddress.ip_network(p)).prefixlen == 24
                and network.subnet_of(ipaddress.ip_network("198.19.0.0/16"))
            )
        conf = self._frr(peer, "vtysh -c 'show running-config' 2>/dev/null || true")
        found = re.findall(
            rf"network\s+({re.escape(_FLOOD_PREFIX_BASE)}\.\d+\.0/24)", conf
        )
        if found:
            return sorted(set(found))
        return []

    def _asn(self, host: str) -> int:
        ops = native_fault_ops(self, host, "bgp_asn", frr_fallback=True)
        return (
            ops.bgp_asn()
            if ops is not None
            else self.runtime.frr_get_bgp_asn_number(host)
        )


class PrefixFaultSupport(BGPPrefixOperations):
    """FRR target selection and readback shared by the independent causes."""

    TAGS = ["bgp", "isp"]
    COMPATIBLE_COLUMNS = frozenset(
        f"{scenario}/ebgp" for scenario in list_isp_base_scenarios()
    )
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}
    supported_backends = ("kathara",)
    native_router_operations = frozenset({"frr_shell"})
    BENCHMARK_TARGETS = "canonical"
    symptom_probe = "custom"
    symptom_class = "control_plane"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.problems.routing_control_plane.bgp import _compile_isp_bgp_inventory
        from nika.net_env.isp.inject_targets import customer_route_session
        from nika.problems.support.benchmark_targets import loaded_inventory

        inventory = loaded_inventory(ctx.net_env).get("bgp")
        if not inventory:
            _, inventory = _compile_isp_bgp_inventory(
                ctx.scenario,
                igp="ospf",
                bgp_mode="ebgp",
                backend=(ctx.isp_options or {}).get("backend"),
                device_profile=(ctx.isp_options or {}).get("device_profile"),
            )
        return customer_route_session(inventory)

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        expected = cls.benchmark_inject_params(ctx)
        for key, value in expected.items():
            if inject.get(key, value) != value:
                raise ValueError(
                    f"{cls.root_cause_name} requires the declared customer-route session"
                )
        for key in ("receiver_name", "peer_name"):
            require_node_capability(ctx, cls.root_cause_name, inject.get(key), "bgp")

    def _config(self, host):
        return self._frr(host, "vtysh -c 'show running-config'")

    def _limit_line(self, host, neighbor):
        return next(
            (
                line.strip()
                for line in self._config(host).splitlines()
                if re.match(
                    rf"\s*neighbor {re.escape(neighbor)} maximum-prefix \d+", line
                )
            ),
            None,
        )

    def prefix_limit(self, host, neighbor):
        line = self._limit_line(host, neighbor)
        return int(line.split()[3]) if line else None

    def reset_session(self, receiver, neighbor):
        self._frr(receiver, f"vtysh -c 'clear bgp {neighbor}'")

    def customer_restored(self, receiver, peer, neighbor):
        site = self.customer_route(receiver, peer)
        return (
            self._session_established(receiver, neighbor)
            and site is not None
            and self._has_route(receiver, site["prefix"])
        )
