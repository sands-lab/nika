"""Independent BGP prefix-limit configuration and advertisement failures."""

import re

from pydantic import BaseModel, Field

from nika.problems.support.recovery import recovery_checks_health
from nika.net_env.isp.inject_targets import _business_originated
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.bgp_prefix import PrefixFaultSupport, _FLOOD_PREFIX_LIST
from nika.problems.support.frr import frr_config_command
from nika.problems.support.oracle import Check, OraclePlaybook, contains, shell, vtysh
from nika.problems.support.polling import wait_until


def _headroom_evidence(output, normal_count):
    limit = re.search(r"Maximum prefixes allowed (\d+)", output)
    received = re.search(r"(\d+) accepted(?:,| prefixes)", output)
    if limit is None:
        return False
    if "BGP state = Established" in output and received is not None:
        return 0 <= int(limit[1]) - int(received[1]) <= 1
    return (
        "peer had exceeded the max. no. of prefixes" in output.lower()
        and normal_count <= int(limit[1]) <= normal_count + 1
    )


class PrefixSessionParams(BaseModel):
    receiver_name: str = Field(description="Router receiving the peer's routes.")
    peer_name: str = Field(description="Peer advertising routes to the receiver.")
    neighbor_ip: str | None = Field(
        default=None,
        description="Peer address at the receiver; resolved from inventory when omitted.",
    )


class BGPLowPrefixLimitParams(PrefixSessionParams):
    maximum_prefix: int | None = Field(
        default=None,
        ge=1,
        description="Terminating prefix limit; defaults to the current received count plus one.",
    )


class BGPPrefixSurgeParams(PrefixSessionParams):
    flood_count: int = Field(
        default=40,
        ge=5,
        le=256,
        description="Additional /24 prefixes advertised from the 198.19.0.0/16 block.",
    )


class BGPLowPrefixLimit(PrefixFaultSupport, ProblemBase):
    combination_only = True
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "bgp_max_prefix_limit_low"
    CONFUSABLE_WITH = ("bgp_prefix_surge",)
    description = (
        "A BGP session's maximum-prefix limit leaves insufficient growth headroom."
    )
    symptom_desc = "An established BGP session has at most one prefix of headroom under its terminating limit."
    Params = BGPLowPrefixLimitParams

    def root_cause_resources(self, params):
        return [node_resource(params.receiver_name)]

    def inject_fault(self, params):
        receiver, peer, neighbor = self._resolve_session(params)
        neighbor = str(neighbor)
        if not self._wait_session_state(
            receiver, neighbor, established=True, timeout_s=180
        ):
            raise RuntimeError("The target eBGP session must be Established.")
        received = self._received_prefix_count(receiver, neighbor)
        limit = (
            params.maximum_prefix if params.maximum_prefix is not None else received + 1
        )
        if received < 1 or not received <= limit <= received + 1:
            raise ValueError(
                "The limit must equal the received count or exceed it by one."
            )
        self._limit_state = {
            "receiver": receiver,
            "peer": peer,
            "neighbor": neighbor,
            "asn": self._asn(receiver),
            "original": self._limit_line(receiver, neighbor),
            "limit": limit,
            "received": received,
        }
        self._set_maximum_prefix(receiver, self._limit_state["asn"], neighbor, limit)
        if not self._session_established(receiver, neighbor):
            raise RuntimeError(
                "The low limit must leave the normal session Established."
            )

    def verify_fault(self, params):
        state = getattr(self, "_limit_state", None)
        receiver, _, neighbor = self._resolve_session(params)
        limit = self.prefix_limit(receiver, str(neighbor))
        return build_verify_result(
            self.root_cause_name,
            bool(state) and limit == state["limit"],
            {
                "maximum_prefix": limit,
                "normal_count": state["received"] if state else None,
            },
        )

    def recover_fault(self, params):
        state = getattr(self, "_limit_state", None)
        if not state:
            return {
                "verified": False,
                "details": {"error": "No prefix-limit snapshot is available."},
            }
        receiver, neighbor = state["receiver"], state["neighbor"]
        self._clear_maximum_prefix(receiver, state["asn"], neighbor)
        if state["original"]:
            self._frr(
                receiver,
                frr_config_command(
                    f"router bgp {state['asn']}",
                    "address-family ipv4 unicast",
                    state["original"],
                ),
            )
            self._frr_save(receiver)
        self.reset_session(receiver, neighbor)
        restored = (
            None
            if not recovery_checks_health()
            else wait_until(
                lambda: self.customer_restored(receiver, state["peer"], neighbor),
                180,
                interval_s=2,
            )
        )
        return {
            "verified": restored is not False
            and self._limit_line(receiver, neighbor) == state["original"],
            "details": {
                "session_and_customer_route_restored": restored,
                "maximum_prefix": self.prefix_limit(receiver, neighbor),
            },
        }

    def oracle_playbook(self, params):
        receiver, _, neighbor = self._resolve_session(params)
        inventory = self._bgp_inventory()
        asns = {n["device"]: n["asn"] for n in inventory["nodes"]}
        normal_count = (
            sum(
                asns[o["device"]] != asns[receiver]
                for o in _business_originated(inventory)
            )
            + 1
        )
        return OraclePlaybook(
            finding=f"{receiver}'s terminating prefix limit has insufficient headroom.",
            steps=[
                shell(
                    receiver,
                    vtysh(f"show bgp neighbors {neighbor}"),
                    "Compare the received count with the terminating limit.",
                    Check(
                        "the terminating limit leaves at most one prefix above the normal table",
                        lambda output: _headroom_evidence(output, normal_count),
                    ),
                    evidence=True,
                ),
                shell(
                    receiver,
                    vtysh("show running-config"),
                    "Read the neighbor's terminating maximum-prefix configuration.",
                    contains(
                        rf"neighbor {neighbor} maximum-prefix \d+",
                        "the neighbor has a terminating limit",
                    ),
                    evidence=True,
                ),
            ],
        )


class BGPPrefixSurge(PrefixFaultSupport, ProblemBase):
    combination_only = True
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "bgp_prefix_surge"
    CONFUSABLE_WITH = ("bgp_max_prefix_limit_low",)
    description = "A BGP peer advertises an unexpectedly large additional prefix set."
    symptom_desc = "Receiving BGP tables grow with additional more-specific prefixes; sessions with headroom remain Established."
    Params = BGPPrefixSurgeParams

    def root_cause_resources(self, params):
        return [node_resource(params.peer_name)]

    def inject_fault(self, params):
        receiver, peer, neighbor = self._resolve_session(params)
        neighbor = str(neighbor)
        if not self._wait_session_state(
            receiver, neighbor, established=True, timeout_s=180
        ):
            raise RuntimeError(
                "The target eBGP session must be Established before the surge."
            )
        prefixes = self._flood_prefixes(
            params.flood_count, peer=peer, neighbor_ip=neighbor
        )
        routers = self._bgp_routers(receiver, peer)
        policies = {}
        for host in routers:
            config = self._config(host)
            maps = self._flood_policy_maps(host)
            if _FLOOD_PREFIX_LIST in config or any(
                f"route-map {m} permit 1\n" in config for m in maps
            ):
                raise ValueError(
                    "The additional-prefix policy conflicts with existing configuration."
                )
            policies[host] = maps
        peer_config = self._config(peer)
        if self._discover_flood_prefixes(peer) or any(
            f"ip route {p} Null0" in peer_config for p in prefixes
        ):
            raise ValueError(
                "The peer already originates prefixes in the additional-prefix block."
            )
        self._surge_state = {
            "receiver": receiver,
            "peer": peer,
            "neighbor": neighbor,
            "asn": self._asn(peer),
            "prefixes": prefixes,
            "policies": policies,
            "attempted": [],
        }
        # Apply equivalent support policy to all BGP routers before advertising.
        for host in routers:
            self._surge_state["attempted"].append(host)
            self._install_flood_policy(host)
            self._frr_save(host)
        self._advertise_flood(peer, self._surge_state["asn"], prefixes)

    def verify_fault(self, params):
        state = getattr(self, "_surge_state", None)
        advertised = self._discover_flood_prefixes(params.peer_name)
        return build_verify_result(
            self.root_cause_name,
            bool(state) and set(state["prefixes"]) <= set(advertised),
            {"peer": params.peer_name, "additional_prefixes": advertised},
        )

    def recover_fault(self, params):
        state = getattr(self, "_surge_state", None)
        if not state:
            return {
                "verified": False,
                "details": {"error": "No advertisement snapshot is available."},
            }
        peer = state["peer"]
        self._withdraw_flood(peer, state["asn"], state["prefixes"])
        for host in state["attempted"]:
            lines = [f"no route-map {m} permit 1" for m in state["policies"][host]]
            lines.append(f"no ip prefix-list {_FLOOD_PREFIX_LIST}")
            self._frr(host, frr_config_command(*lines))
            self._frr_save(host)
        # Withdrawing advertisements does not clear FRR's prefix-overflow latch.
        self.reset_session(state["receiver"], state["neighbor"])
        restored = (
            None
            if not recovery_checks_health()
            else wait_until(
                lambda: (
                    self.customer_restored(state["receiver"], peer, state["neighbor"])
                    and all(
                        not self._has_route(state["receiver"], p)
                        for p in state["prefixes"]
                    )
                ),
                180,
                interval_s=2,
            )
        )
        peer_config = self._config(peer)
        clean = not self._discover_flood_prefixes(peer) and not any(
            f"ip route {prefix} Null0" in peer_config for prefix in state["prefixes"]
        )
        for host in state["attempted"]:
            config = self._config(host)
            clean = (
                clean
                and _FLOOD_PREFIX_LIST not in config
                and not any(
                    f"route-map {policy} permit 1\n" in config
                    for policy in state["policies"][host]
                )
            )
        return {
            "verified": restored is not False and clean,
            "details": {
                "advertisements_and_policies_removed": clean,
                "session_and_customer_route_restored": restored,
            },
        }

    def oracle_playbook(self, params):
        receiver, peer, neighbor = self._resolve_session(params)
        # A separate receiver can retain the routes after the low-limit peer resets.
        observers = sorted(
            (s["local_device"], s["remote_ip"])
            for s in self._bgp_inventory()["sessions"]
            if s["remote_device"] == peer
            and s["local_device"] != receiver
            and s["session_type"] == "ebgp"
        )
        observer, address = observers[0] if observers else (receiver, neighbor)
        return OraclePlaybook(
            finding=f"{peer} originates an excessive additional prefix set.",
            steps=[
                shell(
                    peer,
                    vtysh("show running-config"),
                    "Inspect the peer's originated prefix set.",
                    contains(
                        r"network 198\.19\.\d+\.0/24",
                        "the peer originates additional more-specifics",
                    ),
                    evidence=True,
                ),
                shell(
                    observer,
                    vtysh(f"show bgp ipv4 unicast neighbors {address} routes"),
                    "Inspect the additional routes at another receiving peer.",
                    contains(
                        r"198\.19\.\d+\.0/24",
                        "the receiving table contains the additional routes",
                    ),
                    evidence=True,
                ),
            ],
        )
