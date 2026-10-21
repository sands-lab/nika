"""Lower fixed RFC 6811 IPv4 origin validation to Batfish prefix/AS-path tests.

The snapshot assumes the supplied SLURM assertions have synchronized through RTR.
RTR availability and cryptographic repository validation are runtime concerns.
AS paths are ordinary AS_SEQUENCE paths, as in the ISP sketches.
"""

from __future__ import annotations

import re
from ipaddress import IPv4Network


def project_frr_rpki(config, slurm, *, cache):
    if slurm.get("slurmVersion") != 1 or any(
        slurm.get("validationOutputFilters", {}).values()
    ):
        raise ValueError("Projection requires unfiltered local SLURM IPv4 assertions")
    assertions = slurm["locallyAddedAssertions"]["prefixAssertions"]
    vrps = []
    for row in assertions:
        prefix = IPv4Network(row["prefix"])
        maximum = row.get("maxPrefixLength", prefix.prefixlen)
        if not prefix.prefixlen <= maximum <= 32 or not 0 <= row["asn"] <= 4294967295:
            raise ValueError("Invalid IPv4 VRP assertion")
        vrps.append((prefix, maximum, row["asn"]))
    if not vrps or "NIKA-ROV-" in config:
        raise ValueError("Projection requires nonempty VRPs and fresh model names")
    cache_pattern = (
        r"^rpki\n rpki cache tcp "
        + re.escape(cache[0])
        + r" "
        + str(cache[1])
        + r" preference 1\n exit\n!\n"
    )
    config, count = re.subn(cache_pattern, "", config, flags=re.MULTILINE)
    if count != 1 or re.search(
        r"^\s*(?:rpki|match rpki (?!invalid$))", config, re.MULTILINE
    ):
        raise ValueError("RPKI cache or policy is outside the fixed-VRP projection")
    (local_asn,) = re.findall(r"^router bgp ([0-9]+)$", config, re.MULTILINE)
    local_asn = int(local_asn)
    asns = sorted({asn for _, _, asn in vrps if asn})
    names = []
    definitions = []
    for asn in asns:
        name = f"NIKA-ROV-{asn}"
        names.append(name)
        # Prefix-list deny means the route does not match the invalid set.
        # Any covering authorization is sufficient, including overlapping VRPs.
        valid = [(p, m) for p, m, a in vrps if a == asn]
        for seq, (p, m) in enumerate(valid, 1):
            definitions.append(f"ip prefix-list {name} seq {seq} deny {p} le {m}")
        for seq, (p, _, _) in enumerate(vrps, len(valid) + 1):
            definitions.append(f"ip prefix-list {name} seq {seq} permit {p} le 32")
        definitions.append(rf"bgp as-path access-list {name} permit (^|[^0-9]){asn}$")
        if local_asn == asn:
            definitions.append(f"bgp as-path access-list {name} permit ^$")
    name = "NIKA-ROV-OTHER"
    names.append(name)
    for seq, (p, _, _) in enumerate(vrps, 1):
        definitions.append(f"ip prefix-list {name} seq {seq} permit {p} le 32")
    for asn in asns:
        definitions.append(rf"bgp as-path access-list {name} deny (^|[^0-9]){asn}$")
    if local_asn in asns:
        definitions.append(f"bgp as-path access-list {name} deny ^$")
    definitions.append(f"bgp as-path access-list {name} permit .*")
    pattern = r"^route-map (\S+) deny ([0-9]+)\n match rpki invalid\n!"
    matches = list(re.finditer(pattern, config, re.MULTILINE))
    if config.count("match rpki invalid") != len(matches):
        raise ValueError("Only standalone invalid-origin deny clauses are modeled")
    if len({m[1] for m in matches}) != len(matches):
        raise ValueError(
            "Projection requires at most one invalid-origin clause per policy"
        )
    # Expand one clause into disjoint origin classes without changing its order.
    for match in reversed(matches):
        policy, seq = match[1], int(match[2])
        factor = len(names) + 1
        config = re.sub(
            r"^route-map " + re.escape(policy) + r" (permit|deny) ([0-9]+)$",
            lambda m: f"route-map {policy} {m[1]} {int(m[2]) * factor}",
            config,
            flags=re.MULTILINE,
        )
        old = f"route-map {policy} deny {seq * factor}\n match rpki invalid\n!"
        replacement = "\n".join(
            f"route-map {policy} deny {seq * factor + i}\n match ip address prefix-list {n}\n match as-path {n}\n!"
            for i, n in enumerate(names)
        )
        config = config.replace(old, replacement, 1)
    return "\n".join(definitions) + "\n!\n" + config
