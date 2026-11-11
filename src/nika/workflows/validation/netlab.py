"""Generate netlab candidates and bind runtime controls to their command syntax."""

import re

from nika.net_env.isp.igp.frr import ISIS_PROCESS


def analysis_device(device_profile: str, igp: str = "ospf") -> str:
    """Return the syntax Batfish analyzes for a deployment's routers.

    Batfish parses FRR and IOS XR but models IS-IS only for IOS XR. Other
    deployments are analyzed through their design rendered by netlab: FRR for
    OSPF, IOS XR for IS-IS.
    """
    return "iosxr" if device_profile == "iosxr" or igp == "isis" else "frr"


def render_design_for_analysis(spec, env, parameters, directory, changes=()):
    """Render routers that Batfish cannot parse from their routing design.

    Returns ``(configs, device)`` for those routers, or ``({}, None)`` when
    Batfish parses every deployed router. ``changes`` are design changes of a
    negative control, rendered into the configurations.
    """
    if spec.routing_model in {"srl-isp", "routeros-isp", "frr-isis"}:
        from nika.net_env.isp.netlab import generate_netlab_configs

        device = analysis_device(env.device_profile, env.plan.igp)
        configs = generate_netlab_configs(
            env.plan,
            env.bgp_plan,
            env.device_profile,
            directory,
            render_device=device,
            changes=changes,
        )
        return configs, device
    if spec.routing_model == "srl-bgp":
        from nika.net_env.synthesis.netlab_srl import generate_srl_configs

        configs = generate_srl_configs(
            env, parameters, directory, as_frr=True, changes=changes
        )
        return configs, "frr"
    if spec.routing_model == "routeros-bgp":
        from nika.net_env.synthesis.netlab_direct import generate_direct_bgp_configs

        configs = generate_direct_bgp_configs(
            env, parameters, directory, as_frr=True, changes=changes
        )
        return configs, "frr"
    if getattr(env, "native_routers", None) and spec.routing_model == "frr":
        from nika.net_env.native_routers import design_analyzed_routers
        from nika.net_env.synthesis.netlab_kathara import generate_kathara_configs

        analyzed = design_analyzed_routers(env)
        if analyzed:
            rendered = generate_kathara_configs(
                env, parameters, directory, as_frr=True, changes=changes
            )
            return {name: rendered[name] for name in analyzed}, "frr"
    return {}, None


def _bind_routeros_runtime(controls, configs):
    """Address netlab's RouterOS object names in native runtime controls."""
    bound = []
    for control in controls:
        config = configs[control.node]
        if control.name == "business-withdrawal" and "list=bgp-networks" in config:
            control = control.model_copy(
                update={
                    key: tuple(
                        c.replace("list=BGP-ORIGINS", "list=bgp-networks")
                        for c in getattr(control, key)
                    )
                    for key in ("commands", "recovery")
                }
            )
        elif control.name == "loopback-withdrawal":
            (template,) = re.findall(
                r"^/routing/ospf/interface-template add .*interface=loopback .*?$",
                config,
                re.M,
            )
            control = control.model_copy(
                update={
                    "commands": (
                        "/routing/ospf/interface-template remove [find where interfaces=loopback]",
                    ),
                    "recovery": (template,),
                }
            )
        bound.append(control)
    return tuple(bound)


def check_netlab_scope(spec, env):
    """Check the same supported construction scope in preparation and execution."""
    from nika.net_env.isp.igp.plan import IspPlan
    from nika.net_env.isp.netlab import netlab_topology

    if getattr(env, "device_profile", None) == "sonic":
        raise ValueError("netlab does not generate SONiC CONFIG_DB and FRR files")
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
        from nika.net_env.synthesis.netlab_kathara import kathara_topology

        kathara_topology(env)


def retained_capabilities(spec, env) -> list[dict[str, str]]:
    """List the capabilities NIKA renders beside netlab output, with reasons."""
    if getattr(env, "routing_design", None) is not None:
        from nika.net_env.synthesis.netlab_kathara import (
            retained_capabilities as design_capabilities,
        )

        return design_capabilities(env)
    retained = []
    if spec.routing_model in {"srl-bgp", "srl-isp"}:
        retained.append(
            (
                "existing IPv6 interface addressing"
                if spec.routing_model == "srl-bgp"
                else "IGP ECMP capacity from the completed sketch",
                "the pinned netlab SR Linux templates do not emit this attribute",
            )
        )
    bgp_plan = getattr(env, "bgp_plan", None)
    if spec.routing_model == "routeros-bgp" or (
        getattr(env, "device_profile", None) == "routeros" and bgp_plan
    ):
        retained.append(
            (
                "BGP origins, exact-prefix admission and local-preference filters",
                "netlab's routeros7 device declares no BGP advertise, prefix-list "
                "or routing policy feature",
            )
        )
    elif bgp_plan and any(p.session_type == "ibgp" for p in bgp_plan.sessions):
        retained.append(
            (
                "iBGP attachment of upstream-generated admission policies",
                "netlab's bgp.policy plugin binds policies to external peers only",
            )
        )
    if getattr(env, "rpki", False) or getattr(env, "rtbh", False):
        retained.append(
            (
                "RPKI/RTBH security policy and runtime attachments",
                "netlab has no RPKI or RTBH module",
            )
        )
    return [{"capability": c, "reason": r} for c, r in retained]


def generate_configs(spec, env, parameters, directory, changes=()):
    """Compile the declared routing capabilities through one upstream runner.

    ``changes`` are design changes of a negative control, rendered into the
    configurations.
    """
    from nika.net_env.isp.igp.plan import IspPlan

    if isinstance(getattr(env, "plan", None), IspPlan):
        from nika.net_env.isp.netlab import generate_isp_configs

        return generate_isp_configs(env, directory, changes)
    if hasattr(env, "BGP_ROUTERS"):
        from nika.net_env.synthesis.netlab_direct import generate_direct_bgp_configs

        return generate_direct_bgp_configs(env, parameters, directory, changes=changes)
    if env.backend == "containerlab":
        from nika.net_env.synthesis.netlab_srl import generate_srl_configs

        return generate_srl_configs(env, parameters, directory, changes=changes)
    from nika.net_env.synthesis.netlab_kathara import generate_kathara_configs

    return generate_kathara_configs(env, parameters, directory, changes=changes)


def bind_runtime_controls(spec, configs, device_profile):
    """Address the generated configurations' syntax in runtime counterexamples.

    Static counterexamples and fault models state design changes that netlab
    renders, so only the device commands of runtime controls need binding.
    """
    runtime = tuple(spec.runtime_counterexamples)
    if device_profile == "nokia_srlinux":
        from nika.workflows.validation.netlab_srl import bind_srl_runtime

        runtime = bind_srl_runtime(runtime, configs)
    elif device_profile == "routeros":
        runtime = _bind_routeros_runtime(runtime, configs)
    else:
        runtime = tuple(
            _bind_loopback_withdrawal(control, configs[control.node], device_profile)
            for control in runtime
        )
    return spec.model_copy(update={"runtime_counterexamples": runtime})


def _bind_loopback_withdrawal(control, config, device_profile):
    """Withdraw the loopback with netlab's IGP commands instead of NIKA's."""
    if control.name != "loopback-withdrawal":
        return control
    if device_profile == "iosxr":
        context = {"router ospf CORE": "router ospf 1", "area 0": "area 0.0.0.0"}
        return control.model_copy(
            update={
                key: tuple(context.get(c, c) for c in getattr(control, key))
                for key in ("commands", "recovery")
            }
        )
    blocks = re.findall(r"^interface lo\n(?:(?: [^\n]*|![^\n]*|)\n)*", config, re.M)
    if any(f" ip router isis {ISIS_PROCESS}\n" in b for b in blocks):
        return control
    (block,) = [b for b in blocks if " ip ospf area " in b]
    (area,) = re.findall(r"^ ip ospf area (\S+)$", block, re.M)
    base = ("configure terminal", "interface lo")
    return control.model_copy(
        update={
            "commands": (*base, f"no ip ospf area {area}", "end"),
            "recovery": (*base, f"ip ospf area {area}", "end"),
        }
    )
