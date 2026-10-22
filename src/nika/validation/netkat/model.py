"""Finite match/action forwarding relations, independent of the device backend."""

from ipaddress import IPv4Network

from pydantic import Field

from nika.net_env.contract import ContractModel


class ForwardingEndpoint(ContractModel):
    address: str
    switch: str
    mac: str


class ForwardingAction(ContractModel):
    peer: str
    destination_mac: str


class ForwardingRule(ContractModel):
    prefix: str
    priority: int
    actions: tuple[ForwardingAction, ...]


class ForwardingNetwork(ContractModel):
    endpoints: dict[str, ForwardingEndpoint]
    rules: dict[str, tuple[ForwardingRule, ...]]
    disabled_links: tuple[tuple[str, str], ...] = ()
    assumptions: tuple[str, ...] = ()
    metadata: dict = Field(default_factory=dict)

    def location_ids(self):
        return {
            name: i
            for i, name in enumerate(
                sorted(self.rules.keys() | self.endpoints.keys()), 1
            )
        }

    def relation(self):
        """Encode ordered rules and physical attachments as a NetKAT relation.

        Each choice denotes a possible selector output, not a hash distribution.
        Endpoint reception checks the destination MAC; router forwarding itself
        follows the supported IP-only matches. TTL and ARP remain assumptions.
        """
        ids = self.location_ids()
        macs = {
            m: i
            for i, m in enumerate(
                sorted(
                    {e.mac for e in self.endpoints.values()}
                    | {
                        a.destination_mac
                        for rows in self.rules.values()
                        for r in rows
                        for a in r.actions
                    }
                ),
                1,
            )
        }
        disabled = {frozenset(pair) for pair in self.disabled_links}
        terms = []
        for name, endpoint in self.endpoints.items():
            if frozenset((name, endpoint.switch)) not in disabled:
                terms.append(f"@sw={ids[name]} ⋅ @done=0 ⋅ @sw←{ids[endpoint.switch]}")
        for switch, rows in self.rules.items():
            previous = []
            for rule in sorted(rows, key=lambda r: r.priority, reverse=True):
                network = IPv4Network(rule.prefix, strict=False)
                if any(p == rule.priority and network.overlaps(n) for p, n in previous):
                    raise ValueError(
                        f"Ambiguous rule priority on {switch}: {rule.priority}"
                    )
                surviving = [network]
                for _, higher in previous:
                    remaining = []
                    for prefix in surviving:
                        if not prefix.overlaps(higher):
                            remaining.append(prefix)
                        elif higher.subnet_of(prefix) and higher != prefix:
                            remaining.extend(prefix.address_exclude(higher))
                    surviving = remaining
                previous.append((rule.priority, network))
                guard = union(prefix_test(str(p)) for p in surviving)
                outputs = []
                for action in rule.actions:
                    if action.peer not in ids:
                        raise ValueError(f"Unknown output peer: {action.peer}")
                    if frozenset((switch, action.peer)) in disabled:
                        continue
                    reception = ""
                    endpoint = self.endpoints.get(action.peer)
                    if endpoint:
                        reception = f" ⋅ @mac={macs[endpoint.mac]} ⋅ @done←1"
                    outputs.append(
                        f"@mac←{macs[action.destination_mac]} ⋅ @sw←{ids[action.peer]}{reception}"
                    )
                terms.append(
                    f"@sw={ids[switch]} ⋅ @done=0 ⋅ ({guard}) ⋅ ({union(outputs)})"
                )
        return union(terms)


def union(expressions):
    return " + ".join(f"({e})" for e in expressions) or "⊥"


def prefix_test(prefix):
    network = IPv4Network(prefix, strict=False)
    bits = f"{int(network.network_address):032b}"
    return (
        " ⋅ ".join(f"@d{i}={bit}" for i, bit in enumerate(bits[: network.prefixlen]))
        or "⊤"
    )
