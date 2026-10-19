"""Bind existing negative controls to netlab's concrete command syntax."""

import re


def check_netlab_scope(spec, env):
    """Check the same supported construction scope in preparation and execution."""
    from nika.net_env.isp.igp.plan import IspPlan
    from nika.net_env.isp.netlab import netlab_topology

    if env.backend != "kathara" or not isinstance(getattr(env, "plan", None), IspPlan):
        raise ValueError("netlab construction requires a supported Kathara ISP plan")
    netlab_topology(env.plan, env.bgp_plan, env.device_profile)
    if spec.failure_checks:
        raise ValueError(
            "netlab registered configuration-failure bindings are not covered; "
            "use declared negative controls and link incidents"
        )


def bind_netlab_controls(spec, configs, device_profile):
    """Keep original violation sets, changing only concrete mutation bindings.

    Custom fault bindings must already match the emitted candidate; unsupported
    syntax is rejected by the regular experiment rather than skipped.
    """
    static = []
    runtime = list(spec.runtime_counterexamples)
    for mutation in spec.counterexamples:
        config = configs[mutation.node]
        if config.count(mutation.old) == 1:
            static.append(mutation)
            continue
        old, new = mutation.old, mutation.new
        if mutation.name == "missing-loopback-announcement":
            if device_profile == "iosxr":
                old, new = "  interface Loopback0\n", ""
                context = {
                    "router ospf NIKA": "router ospf 1",
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
                (old,) = re.findall(
                    rf"^ neighbor {peer}\n.*?(?=^!|\Z)", config, re.M | re.S
                )
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
        }
    )
