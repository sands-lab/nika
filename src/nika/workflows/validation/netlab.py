"""Bind existing negative controls to netlab's concrete command syntax."""

import re
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from nika.net_env.isp.igp.frr import ISIS_PROCESS


def _ospf_binding(config, old, new):
    """Bind a network-statement edit to its emitted interface declaration."""
    match = re.fullmatch(r"\s*network ([0-9./]+) area ([0-9.]+)\s*", old)
    if not match:
        return old, new
    prefix, area = match.groups()
    area = str(IPv4Address(int(area) if "." not in area else area))
    blocks = list(
        re.finditer(r"^interface (\S+)\n(?:(?: [^\n]*|![^\n]*|)\n)*", config, re.M)
    )
    addresses = {}
    for block in blocks:
        for address in re.findall(r"^ ip address (\S+)$", block[0], re.M):
            addresses[block[1]] = IPv4Interface(address).ip
    (block,) = [
        b[0]
        for b in blocks
        if b[1] in addresses
        and addresses[b[1]] in IPv4Network(prefix)
        and f" ip ospf area {area}\n" in b[0]
    ]
    replacement = re.fullmatch(r"\s*network ([0-9./]+) area ([0-9.]+)\s*", new)
    if replacement and replacement[1] != prefix:
        raise ValueError("OSPF binding changes must preserve the selected interface")
    command = ""
    if replacement:
        new_area = replacement[2]
        command = (
            " ip ospf area "
            + str(IPv4Address(int(new_area) if "." not in new_area else new_area))
            + "\n"
        )
    return block, block.replace(f" ip ospf area {area}\n", command)


def check_netlab_scope(spec, env):
    """Check the same supported construction scope in preparation and execution."""
    from nika.net_env.isp.igp.plan import IspPlan
    from nika.net_env.isp.netlab import netlab_topology

    if isinstance(getattr(env, "plan", None), IspPlan):
        if env.backend not in {"kathara", "containerlab"}:
            raise ValueError("netlab ISP construction requires a supported backend")
        netlab_topology(env.plan, env.bgp_plan, env.device_profile)
    elif hasattr(env, "BGP_ROUTERS"):
        from nika.net_env.synthesis.netlab_direct import direct_bgp_topology

        direct_bgp_topology(env)
    elif env.backend == "containerlab":
        from nika.net_env.synthesis.netlab_srl import srl_topology

        srl_topology(env)
    else:
        if getattr(env, "native_routers", None):
            # The Ethernet adapters hand netlab FRR router sketches only.
            raise ValueError(
                "netlab Ethernet construction covers FRR routers; "
                f"device_profile={env.device_profile} routers are rendered natively"
            )
        if "import_preferences" in env.ROUTING_PARAMETER_KINDS:
            from nika.net_env.synthesis.netlab_overlay import overlay_topology

            overlay_topology(env)
        else:
            from nika.net_env.synthesis.netlab_kathara import kathara_topology

            kathara_topology(env)


def _bind_frr_failure_edit(config, old, new):
    """Resolve original FRR edits against upstream-generated interface/BGP CLI."""
    if config.count(old) == 1:
        return old, new

    old_map = re.fullmatch(r"route-map BGP-OUT (deny|permit) (\d+)\n?", old)
    if old_map:
        actual_maps = {
            policy
            for policy in re.findall(
                r"^\s*neighbor \S+ route-map (\S+) out$", config, re.M
            )
            if re.search(
                rf"^route-map {re.escape(policy)} {old_map[1]} {old_map[2]}$",
                config,
                re.M,
            )
        }
        if len(actual_maps) == 1:
            policy = actual_maps.pop()
            candidate_old = old.replace("BGP-OUT", policy)
            candidate_new = new.replace("BGP-OUT", policy)
            if config.count(candidate_old) == 1:
                return candidate_old, candidate_new

    bgp = re.fullmatch(r"router bgp (\d+)\n", old)
    replacement = re.fullmatch(r"router bgp (\d+)\n", new)
    if bgp and replacement and config.count(old) > 1:
        asn = bgp[1]
        headers = re.findall(r"^router bgp (\d+)$", config, re.M)
        if not headers or any(value != asn for value in headers):
            raise ValueError("FRR ASN fault does not identify same-AS router stanzas")
        updated = re.sub(
            rf"^router bgp {re.escape(asn)}$",
            f"router bgp {replacement[1]}",
            config,
            flags=re.M,
        )
        return config, updated

    old_networks = re.findall(
        r"^\s*network\s+(\S+(?:\s+\S+)?)\s+area\s+(\S+)\s*$", old, re.M
    )
    new_networks = re.findall(
        r"^\s*network\s+(\S+(?:\s+\S+)?)\s+area\s+(\S+)\s*$", new, re.M
    )
    if old_networks and len(old_networks) == len(new_networks):
        new_by_prefix = dict(new_networks)
        if set(new_by_prefix) != {prefix for prefix, _ in old_networks}:
            raise ValueError("OSPF area fault changes its selected network prefixes")
        blocks = list(
            re.finditer(r"^interface (\S+)\n(?:(?: [^\n]*|![^\n]*|)\n)*", config, re.M)
        )
        addresses = {}
        areas = {}
        for block in blocks:
            addresses.setdefault(block[1], set()).update(
                IPv4Interface(value).ip
                for value in re.findall(r"^ ip address (\S+)$", block[0], re.M)
            )
            areas.setdefault(block[1], []).extend(
                re.findall(r"^ ip ospf area (\S+)$", block[0], re.M)
            )
        edits = {}
        for prefix, old_area in old_networks:
            network = _frr_network(prefix)
            area = str(IPv4Address(int(old_area) if "." not in old_area else old_area))
            selected_names = {
                name
                for name, values in addresses.items()
                if any(value in network for value in values)
                and area in areas.get(name, ())
            }
            if not selected_names:
                raise ValueError("OSPF area fault has no matching emitted interface")
            next_area = new_by_prefix[prefix]
            next_area = str(
                IPv4Address(int(next_area) if "." not in next_area else next_area)
            )
            for block in blocks:
                if block[1] in selected_names and f" ip ospf area {area}\n" in block[0]:
                    edits[block.start(), block.end()] = block[0].replace(
                        f" ip ospf area {area}\n", f" ip ospf area {next_area}\n"
                    )
        updated = config
        for (start, end), block in sorted(edits.items(), reverse=True):
            updated = updated[:start] + block + updated[end:]
        if updated == config:
            raise ValueError("OSPF area fault did not change emitted interfaces")
        return config, updated
    raise ValueError("Original FRR fault edit does not match supported emitted syntax")


def _frr_network(prefix):
    """Convert FRR's address/wildcard or CIDR network notation to IPv4Network."""
    parts = prefix.split()
    if len(parts) == 1:
        return IPv4Network(parts[0], strict=False)
    if len(parts) == 2:
        wildcard = IPv4Address(parts[1])
        mask = IPv4Address(int(wildcard) ^ 0xFFFFFFFF)
        return IPv4Network((IPv4Address(parts[0]), str(mask)), strict=False)
    raise ValueError("Unsupported FRR OSPF network statement")


def _frr_bgp_network_line(config, old):
    match = re.fullmatch(r"\s*network ([0-9./]+)\s*", old)
    if not match:
        raise ValueError("Business announcement mutation has no BGP network prefix")
    prefix = match[1]
    bgp = af = False
    found = []
    for line in config.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("router bgp "):
            bgp, af = True, False
        elif not line[:1].isspace() and stripped and not stripped.startswith("!"):
            bgp, af = False, False
        elif bgp and stripped in {
            "address-family ipv4",
            "address-family ipv4 unicast",
        }:
            af = True
        elif bgp and stripped == "exit-address-family":
            af = False
        elif bgp and af and re.fullmatch(rf"network {re.escape(prefix)}", stripped):
            found.append(line)
    if len(found) != 1:
        raise ValueError("Business announcement does not identify one emitted BGP line")
    return found[0]


def generate_configs(spec, env, parameters, directory):
    """Compile the declared routing capabilities through one upstream runner."""
    from nika.net_env.isp.igp.plan import IspPlan

    if isinstance(getattr(env, "plan", None), IspPlan):
        from nika.net_env.isp.netlab import generate_netlab_configs

        generated = generate_netlab_configs(
            env.plan, env.bgp_plan, env.device_profile, directory
        )
        if env.backend == "containerlab":
            from nika.net_env.synthesis.netlab_srl import retain_srl_attributes

            generated = retain_srl_attributes(generated, env.routing_documents)
        return generated
    if hasattr(env, "BGP_ROUTERS"):
        from nika.net_env.synthesis.netlab_direct import generate_direct_bgp_configs

        return generate_direct_bgp_configs(env, parameters, directory)
    if env.backend == "containerlab":
        from nika.net_env.synthesis.netlab_srl import generate_srl_configs

        return generate_srl_configs(env, parameters, directory)
    from nika.net_env.synthesis.netlab_kathara import generate_kathara_configs

    if "import_preferences" in env.ROUTING_PARAMETER_KINDS:
        from nika.net_env.synthesis.netlab_overlay import generate_overlay_configs

        return generate_overlay_configs(env, directory)
    return generate_kathara_configs(env, parameters, directory)


def bind_netlab_controls(spec, configs, device_profile):
    """Keep original violation sets, changing only concrete mutation bindings.

    Custom fault bindings must already match the emitted candidate; unsupported
    syntax is rejected by the regular experiment rather than skipped.
    """
    if device_profile == "nokia_srlinux":
        from nika.workflows.validation.netlab_srl import bind_srl_controls

        spec = bind_srl_controls(spec, configs)
    failures = []
    for failure in spec.failure_checks:
        edits = dict(failure.static_edits)
        if device_profile == "frr":
            for node, (old, new) in edits.items():
                edits[node] = _bind_frr_failure_edit(configs[node], old, new)
        else:
            for node, (old, _) in edits.items():
                if configs[node].count(old) != 1:
                    raise ValueError(
                        "A modeled fault must identify one concrete statement per node"
                    )
        failures.append(failure.model_copy(update={"static_edits": edits}))
    static = []
    runtime = list(spec.runtime_counterexamples)
    for mutation in spec.counterexamples:
        config = configs[mutation.node]
        if config.count(mutation.old) == 1:
            static.append(mutation)
            continue
        old, new = mutation.old, mutation.new
        if (
            device_profile == "routeros"
            and mutation.name == "missing-business-announcement"
        ):
            match = re.search(r"(?:network|address)=([0-9./]+)\s*$", mutation.old)
            if not match:
                raise ValueError(
                    "RouterOS business announcement mutation has no prefix"
                )
            prefix = match[1]
            (old,) = re.findall(
                rf"^/ip/firewall/address-list add list=bgp-networks address={re.escape(prefix)}$",
                config,
                re.M,
            )
            new = ""
            for i, control in enumerate(runtime):
                if control.name == "business-withdrawal":
                    runtime[i] = control.model_copy(
                        update={
                            "commands": tuple(
                                command.replace("list=BGP-ORIGINS", "list=bgp-networks")
                                for command in control.commands
                            ),
                            "recovery": tuple(
                                command.replace("list=BGP-ORIGINS", "list=bgp-networks")
                                for command in control.recovery
                            ),
                        }
                    )
        elif (
            device_profile == "frr" and mutation.name == "missing-business-announcement"
        ):
            old = _frr_bgp_network_line(config, old)
            if new.strip():
                raise ValueError(
                    "Business withdrawal must remove the selected BGP line"
                )
            new = ""
        elif (
            device_profile == "routeros"
            and mutation.name == "missing-loopback-announcement"
        ):
            (old,) = re.findall(
                r"^/routing/ospf/interface-template add .*interface=loopback .*?$",
                config,
                re.M,
            )
            new = ""
            for i, control in enumerate(runtime):
                if control.name == "loopback-withdrawal":
                    runtime[i] = control.model_copy(
                        update={
                            "commands": (
                                "/routing/ospf/interface-template remove [find where interfaces=loopback]",
                            ),
                            "recovery": (old,),
                        }
                    )
        elif device_profile == "routeros" and mutation.name == "missing-rr-client-role":
            adjacency = next(
                i.adjacency
                for i in spec.healthy.intents
                if i.id in mutation.expected_failed_ids
            )
            peer = adjacency.remote_address.replace(".", "_")
            (old,) = re.findall(
                rf"^/routing/bgp/connection set .*name={re.escape(peer)}\].*local.role=ibgp-rr$",
                config,
                re.M,
            )
            new = old.replace("local.role=ibgp-rr", "local.role=ibgp")
        elif re.fullmatch(r"\s*network [0-9./]+ area [0-9.]+\s*", old):
            old, new = _ospf_binding(config, old, new)
        elif mutation.name == "missing-loopback-announcement":
            if device_profile == "iosxr":
                # Remove the whole passive loopback binding from the IGP.
                (old,) = re.findall(
                    r"^  interface Loopback0\n   passive enable\n"
                    r"|^ interface Loopback0\n  passive\n  address-family ipv4\n",
                    config,
                    re.M,
                )
                new = ""
                context = {
                    "router ospf CORE": "router ospf 1",
                    "area 0": "area 0.0.0.0",
                }
                for i, control in enumerate(runtime):
                    if control.name == "loopback-withdrawal":
                        runtime[i] = control.model_copy(
                            update={
                                "commands": tuple(
                                    context.get(s, s) for s in control.commands
                                ),
                                "recovery": tuple(
                                    context.get(s, s) for s in control.recovery
                                ),
                            }
                        )
            else:
                blocks = re.findall(
                    r"^interface lo\n(?:(?: [^\n]*|![^\n]*|)\n)*", config, re.M
                )
                isis = [b for b in blocks if f" ip router isis {ISIS_PROCESS}\n" in b]
                if isis:
                    (old,) = isis
                    new = re.sub(
                        rf"^ (?:ip router isis {ISIS_PROCESS}|isis passive)\n",
                        "",
                        old,
                        flags=re.M,
                    )
                    static.append(mutation.model_copy(update={"old": old, "new": new}))
                    continue
                (old,) = [b for b in blocks if " ip ospf area " in b]
                new = re.sub(r"^ ip ospf area [^\n]*\n", "", old, flags=re.M)
                (area,) = re.findall(r"^ ip ospf area (\S+)$", old, re.M)
                for i, control in enumerate(runtime):
                    if control.name == "loopback-withdrawal":
                        base = ("configure terminal", "interface lo")
                        runtime[i] = control.model_copy(
                            update={
                                "commands": (*base, f"no ip ospf area {area}", "end"),
                                "recovery": (*base, f"ip ospf area {area}", "end"),
                            }
                        )
        elif mutation.name == "missing-rr-client-role":
            adjacency = next(
                i.adjacency
                for i in spec.healthy.intents
                if i.id in mutation.expected_failed_ids
            )
            peer = re.escape(adjacency.remote_address)
            if device_profile == "iosxr":
                (old,) = [
                    block
                    for block in re.findall(
                        rf"^ neighbor {peer}\n.*?(?=^!|\Z)", config, re.M | re.S
                    )
                    if "   route-reflector-client\n" in block
                ]
                new = old.replace("   route-reflector-client\n", "")
            else:
                (old,) = re.findall(
                    rf"^ +neighbor {peer} route-reflector-client$", config, re.M
                )
                new = ""
        static.append(mutation.model_copy(update={"old": old, "new": new}))
    return spec.model_copy(
        update={
            "counterexamples": tuple(static),
            "runtime_counterexamples": tuple(runtime),
            "failure_checks": tuple(failures),
        }
    )
