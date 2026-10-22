"""Normalize the bounded upstream SR Linux syntax for concrete IPv4 analysis.

The configuration sent to SR Linux is unchanged. This adapter expands inherited
attributes and validates upstream's internal policy scaffolding before using the
existing concrete projection; it does not reconstruct a configuration from intent.
"""

from copy import deepcopy
from ipaddress import IPv4Address, IPv4Interface, IPv6Interface


def _only(row, allowed):
    unknown = set(row) - set(allowed)
    if unknown:
        raise ValueError(f"Unsupported SR Linux attributes: {sorted(unknown)}")


def _ipv4_families(rows):
    active = []
    for row in rows:
        row = deepcopy(row)
        _only(
            row,
            {
                "afi-safi-name",
                "admin-state",
                "send-community-type",
                "import-policy",
                "export-policy",
            },
        )
        if (
            row["afi-safi-name"] == "ipv6-unicast"
            and row.get("admin-state") == "disable"
        ):
            continue
        if row["afi-safi-name"] != "ipv4-unicast" or row.get("admin-state") != "enable":
            raise ValueError("Only enabled IPv4 and disabled IPv6 families are modeled")
        if set(row.pop("send-community-type", [])) - {"standard", "large", "extended"}:
            raise ValueError("Unsupported BGP community family")
        active.append(row)
    if len(active) != 1:
        raise ValueError("Expected exactly one IPv4 address family")
    return active


def normalize_netlab_srl(document):
    """Expand the pinned tool's finite IPv4 template subset, failing closed."""
    document = deepcopy(document)
    (instance,) = document["srl_nokia-network-instance:network-instance"]
    if "type" not in instance:
        return document
    if instance.pop("type") != "default":
        raise ValueError("Only the default network instance is modeled")
    instance.setdefault("admin-state", "enable")
    router_id = instance.pop("router-id", None)
    for interface in document["srl_nokia-interfaces:interface"]:
        interface.pop("description", None)
        interface.setdefault("admin-state", "enable")
        for sub in interface["subinterface"]:
            sub.pop("description", None)
            if ipv6 := sub.pop("ipv6", None):
                _only(
                    ipv6,
                    {
                        "admin-state",
                        "address",
                        "neighbor-discovery",
                        "router-advertisement",
                    },
                )
                if ipv6.get(
                    "neighbor-discovery", {"learn-unsolicited": "link-local"}
                ) != {"learn-unsolicited": "link-local"}:
                    raise ValueError("Unsupported IPv6 neighbor discovery settings")
                if ipv6.get(
                    "router-advertisement",
                    {
                        "router-role": {
                            "admin-state": "enable",
                            "min-advertisement-interval": 4,
                            "max-advertisement-interval": 5,
                        }
                    },
                ) != {
                    "router-role": {
                        "admin-state": "enable",
                        "min-advertisement-interval": 4,
                        "max-advertisement-interval": 5,
                    }
                }:
                    raise ValueError("Unsupported IPv6 router advertisement settings")
                for address in ipv6.get("address", []):
                    _only(address, {"ip-prefix", "primary", "type"})
                    if address.get("type", "global-unicast") != "global-unicast":
                        raise ValueError("Unsupported IPv6 address type")
                    IPv6Interface(address["ip-prefix"])
            for address in sub["ipv4"]["address"]:
                if "primary" in address and address.pop("primary") != [None]:
                    raise ValueError("Unsupported SR Linux primary-address marker")
    protocols = instance["protocols"]
    for key, proto in protocols.items():
        if key == "srl_nokia-ospf:ospf":
            for igp in proto["instance"]:
                igp["router-id"] = igp.get("router-id", router_id)
                if igp["router-id"] is None:
                    raise ValueError("OSPF requires a concrete router ID")
                for area in igp["area"]:
                    for row in area["interface"]:
                        if "failure-detection" in row and row.pop(
                            "failure-detection"
                        ) != {"enable-bfd": False}:
                            raise ValueError("OSPF BFD is outside the projection")
        elif key == "srl_nokia-isis:isis":
            for igp in proto["instance"]:
                # SR Linux v24.10.1 YANG: network-instance/srl_nokia-isis.yang,
                # isis-instance-level configuration, metric-style default wide.
                # SR Linux 24.10 L2 uses wide metrics by default. Preserve any
                # explicit metric style for the existing strict validator.
                igp.setdefault("level", [{"level-number": 2, "metric-style": "wide"}])
                for row in igp["interface"]:
                    for family in ("ipv4-unicast", "ipv6-unicast"):
                        if family not in row:
                            continue
                        value = row.pop(family)
                        permitted = {
                            "admin-state": "enable"
                            if family.startswith("ipv4")
                            else "disable"
                        }
                        if value not in (permitted, {**permitted, "enable-bfd": False}):
                            raise ValueError(
                                "Unsupported IS-IS address-family settings"
                            )
                    if (
                        row.get("circuit-type") == "broadcast"
                        and row.get("passive") is True
                    ):
                        row.pop("circuit-type")
                    if "level" in row:
                        levels = row["level"]
                        row["level"] = [
                            level
                            for level in levels
                            if level != {"level-number": 1, "disable": True}
                        ]
        elif key != "srl_nokia-bgp:bgp":
            raise ValueError(f"Unsupported SR Linux protocol {key}")
    if "srl_nokia-bgp:bgp" not in protocols:
        return document
    bgp = protocols["srl_nokia-bgp:bgp"]
    # A global IPv6 family alone establishes no IPv6 exchange. Every concrete
    # group and peer is still checked below to have no enabled IPv6 family.
    global_families = bgp["afi-safi"]
    for family in global_families:
        if family["afi-safi-name"] == "ipv6-unicast":
            _only(family, {"afi-safi-name", "admin-state"})
            if family["admin-state"] not in {"enable", "disable"}:
                raise ValueError("Unsupported global IPv6 address family")
    bgp["afi-safi"] = _ipv4_families(
        [f for f in global_families if f["afi-safi-name"] != "ipv6-unicast"]
    )
    routing = document["srl_nokia-routing-policy:routing-policy"]
    policies = {p["name"]: p for p in routing["policy"]}
    expected = {
        "name": "default_bgp_export",
        "default-action": {"policy-result": "reject"},
        "statement": [
            {
                "name": "prefixes",
                "match": {"prefix-set": "default_bgp_advertise"},
                "action": {"policy-result": "next-policy"},
            },
            {
                "name": "bgp",
                "match": {"protocol": "bgp"},
                "action": {
                    "policy-result": "next-policy",
                    "bgp": {"communities": {"remove": "ibgp-mark"}},
                },
            },
        ],
    }
    if policies.get("default_bgp_export") != expected:
        raise ValueError("Unsupported upstream BGP origin/export scaffold")
    marking = policies.get("ibgp-mark")
    if marking != {
        "name": "ibgp-mark",
        "default-action": {"policy-result": "reject"},
        "statement": [
            {
                "name": "mark-ibgp-routes",
                "action": {
                    "bgp": {"communities": {"add": "ibgp-mark"}},
                    "policy-result": "accept",
                },
            }
        ],
    }:
        raise ValueError("Unsupported upstream BGP community marking")
    if routing.pop("community-set", []) != [
        {"name": "ibgp-mark", "member": ["65536:0:65536"]}
    ]:
        raise ValueError("Only the tool's internal BGP mark is modeled")
    # The validated export scaffold permits these connected origins and learned
    # BGP routes only. The target model has no other redistribution sources.
    origins = next(
        p["prefix"]
        for p in routing["prefix-set"]
        if p["name"] == "default_bgp_advertise"
    )
    document_origins = {r["ip-prefix"] for r in origins}
    routing["policy"] = [
        p
        for p in routing["policy"]
        if p["name"] not in {"default_bgp_export", "ibgp-mark"}
    ]
    for policy in routing["policy"]:
        _only(policy, {"name", "statement", "default-action"})
        for statement in policy.setdefault("statement", []):
            match = statement.get("match", {})
            if "prefix" in match:
                prefix_match = match.pop("prefix")
                _only(prefix_match, {"prefix-set"})
                match.update(prefix_match)
        default = policy.pop("default-action", {"policy-result": "reject"})
        _only(default, {"policy-result"})
        policy["statement"].append({"name": "netlab-default", "action": default})
    groups = {g["group-name"]: g for g in bgp["group"]}
    cluster_ids = set()
    for group in groups.values():
        _only(
            group,
            {
                "group-name",
                "admin-state",
                "afi-safi",
                "peer-as",
                "transport",
                "route-reflector",
                "timers",
                "send-community",
                "next-hop-self",
            },
        )
        if group.pop("send-community", {"standard": True, "large": True}) != {
            "standard": True,
            "large": True,
        }:
            raise ValueError("Unsupported BGP community propagation")
        if group.pop("next-hop-self", False) is not False:
            raise ValueError("BGP next-hop rewriting is outside the projection")
        timers = group.pop("timers", {})
        _only(timers, {"connect-retry", "minimum-advertisement-interval"})
        group["afi-safi"] = _ipv4_families(group["afi-safi"])
    for peer in bgp["neighbor"]:
        peer.pop("description", None)
        group = groups[peer["peer-group"]]
        peer.setdefault("peer-as", group.get("peer-as"))
        transport = {**group.get("transport", {}), **peer.get("transport", {})}
        _only(transport, {"local-address", "passive-mode"})
        transport.pop("passive-mode", None)
        if "local-address" not in transport:
            candidates = [
                str(IPv4Interface(address["ip-prefix"]).ip)
                for interface in document["srl_nokia-interfaces:interface"]
                for sub in interface["subinterface"]
                for address in sub["ipv4"]["address"]
                if IPv4Address(peer["peer-address"])
                in IPv4Interface(address["ip-prefix"]).network
            ]
            if len(candidates) != 1 or peer["peer-as"] == bgp["autonomous-system"]:
                raise ValueError(
                    "BGP requires an explicit source or a unique direct external subnet"
                )
            transport["local-address"] = candidates[0]
        peer["transport"] = transport
        rr = {**group.get("route-reflector", {}), **peer.get("route-reflector", {})}
        if rr:
            _only(rr, {"client", "cluster-id"})
            if cluster := rr.pop("cluster-id", None):
                cluster_ids.add(cluster)
            peer["route-reflector"] = rr
        inherited = group["afi-safi"][0]
        override = _ipv4_families(peer.get("afi-safi", bgp["afi-safi"]))[0]
        af = {**inherited, **override}
        for direction in ("import", "export"):
            chain = af.get(direction + "-policy", [])
            if direction == "import" and chain == ["ibgp-mark"]:
                chain = ["accept_all"]
            if direction == "export":
                if not chain or chain[0] != "default_bgp_export":
                    raise ValueError("Unsupported BGP export chain")
                chain = chain[1:]
            if len(chain) != 1:
                raise ValueError("Unsupported multi-policy BGP chain")
            af[direction + "-policy"] = chain
        peer["afi-safi"] = [af]
    if len(cluster_ids) > 1:
        raise ValueError(
            "Multiple inherited BGP cluster IDs are outside the projection"
        )
    if cluster_ids:
        bgp["route-reflector"] = {"cluster-id": cluster_ids.pop()}
    # Inheritance has been expanded from the concrete group, never from intent.
    for group in groups.values():
        for attr in ("transport", "route-reflector"):
            group.pop(attr, None)
        group["afi-safi"] = [
            {
                "afi-safi-name": "ipv4-unicast",
                "admin-state": "enable",
                "import-policy": ["accept_all"],
                "export-policy": ["accept_all"],
            }
        ]
    document["_netlab_origins"] = document_origins
    return document
