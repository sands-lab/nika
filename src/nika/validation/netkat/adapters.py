"""Interpret supported concrete OpenFlow and P4Runtime forwarding candidates."""

from ipaddress import IPv4Network

from nika.validation.netkat.model import (
    ForwardingAction,
    ForwardingEndpoint,
    ForwardingNetwork,
    ForwardingRule,
)

ASSUMPTIONS = (
    "Well-formed unfragmented IPv4 ICMP with sufficient TTL and resolved neighbor entries.",
    "Selector choices overapproximate hash outputs; reachability is existential, not universal delivery or load balance.",
    "No controller transitions, queues, telemetry, registers or transport/application semantics in the static model.",
)


def endpoints_from_model(model):
    result = {}
    for ep in model.endpoints:
        switches = [
            s
            for s, ports in model.ports.items()
            if any(p.peer == ep.name for p in ports)
        ]
        if len(switches) != 1:
            raise ValueError(f"Endpoint needs one physical attachment: {ep.name}")
        result[ep.name] = ForwardingEndpoint(
            address=ep.ip, switch=switches[0], mac=ep.mac
        )
    return result


def openflow_network(model, candidate):
    """Single-table IPv4 prefix rules and SELECT groups with explicit outputs."""
    groups = {(g["switch"], g["group_id"]): g for g in candidate["groups"]}
    rules = {s: [] for s in model.ports}

    def actions(switch, text, *, in_group=False):
        if text == "drop" or not text:
            return []
        mac = None
        outputs = []
        for item in text.split(","):
            if item == "dec_ttl" or item.startswith("mod_dl_src:"):
                continue
            if item.startswith("mod_dl_dst:"):
                mac = item.removeprefix("mod_dl_dst:")
            elif item.startswith("output:"):
                port = next(
                    (
                        p
                        for p in model.ports[switch]
                        if p.name == item.removeprefix("output:")
                    ),
                    None,
                )
                if port is None or mac is None:
                    raise ValueError(f"Unresolved OpenFlow output on {switch}: {text}")
                outputs.append(ForwardingAction(peer=port.peer, destination_mac=mac))
            elif item.startswith("group:") and not in_group:
                group = groups[(switch, int(item.removeprefix("group:")))]
                if group["type"] != "select":
                    raise ValueError("Only SELECT groups are supported")
                for bucket in group["buckets"]:
                    outputs.extend(actions(switch, bucket["actions"], in_group=True))
            else:
                raise ValueError(f"Unsupported IPv4 OpenFlow action: {item}")
        return outputs

    for flow in candidate["flows"]:
        fields = flow["match"].split(",") if flow["match"] else []
        if fields and fields[0] == "arp":
            continue  # Disjoint from the explicitly declared IPv4 input domain.
        if flow["table"] != 0 or any(
            f != "ip" and not f.startswith("nw_dst=") for f in fields
        ):
            raise ValueError(f"Unsupported OpenFlow match: {flow['match']}")
        prefixes = [
            f.removeprefix("nw_dst=") for f in fields if f.startswith("nw_dst=")
        ]
        if len(prefixes) > 1:
            raise ValueError("Multiple destination matches")
        rules[flow["switch"]].append(
            ForwardingRule(
                prefix=prefixes[0] if prefixes else "0.0.0.0/0",
                priority=flow["priority"],
                actions=tuple(actions(flow["switch"], flow["actions"])),
            )
        )
    return ForwardingNetwork(
        endpoints=endpoints_from_model(model),
        rules={s: tuple(r) for s, r in rules.items()},
        assumptions=ASSUMPTIONS,
        metadata={"frontend": "openflow-ipv4"},
    )


def p4runtime_network(model, candidate):
    """Project LPM and selector entries under the documented P4 forwarding slice."""
    rules = {}
    for switch, config in candidate["switches"].items():
        members = {m["member_id"]: m for m in config["members"]}
        groups = {g["group_id"]: g for g in config["groups"]}
        if len(members) != len(config["members"]) or len(groups) != len(
            config["groups"]
        ):
            raise ValueError("Duplicate P4Runtime member/group IDs")
        rows = []
        for entry in config["ipv4_lpm"]:
            actions = []
            ids = groups[entry["group_id"]]["member_ids"]
            if not ids:
                raise ValueError("Empty P4 action-selector group")
            for mid in ids:
                member = members[mid]
                port = next(
                    (p for p in model.ports[switch] if p.bmv2_port == member["port"]),
                    None,
                )
                if port is None:
                    raise ValueError("P4Runtime output port has no physical attachment")
                # Resolve the actual numeric port, not the descriptive peer label.
                actions.append(
                    ForwardingAction(peer=port.peer, destination_mac=member["dst_mac"])
                )
            network = IPv4Network(entry["prefix"], strict=False)
            rows.append(
                ForwardingRule(
                    prefix=str(network),
                    priority=network.prefixlen,
                    actions=tuple(actions),
                )
            )
        rules[switch] = tuple(rows)
    return ForwardingNetwork(
        endpoints=endpoints_from_model(model),
        rules=rules,
        assumptions=ASSUMPTIONS
        + (
            "P4 projection covers LPM and validated forwarding-action assignments; parser/control-flow, TTL and extern semantics remain assumptions and native obligations.",
        ),
        metadata={"frontend": "p4runtime-ipv4"},
    )
