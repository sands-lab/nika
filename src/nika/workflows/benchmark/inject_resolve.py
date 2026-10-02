"""Resolve inject parameters when generating benchmark YAML (offline only)."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.base import ProblemBase
from nika.problems.registry import list_avail_problem_instances
from nika.problems.support.benchmark_targets import (
    InjectTargetContext,
    InjectValidationContext,
    all_device_names as _all_device_names,
    choice as _choice,
    device_interfaces as _device_interfaces,
    first as _first,
)
from nika.workflows.benchmark.isp_options import (
    is_isp_base_topology,
    is_isp_scenario,
)

DEFAULT_SEED = 42

_DEVICE_KEYS = (
    "host_name",
    "host_name_2",
    "attacker_device",
    "control_node",
    "node_name",
    "symptom_host",
    "forwarding_device",
)


def _case_rng(
    seed: int,
    scenario: str,
    problem: str,
    topo_size: str,
    isp_key: str = "",
) -> random.Random:
    key = f"{seed}|{scenario}|{problem}|{topo_size}"
    if isp_key:
        key = f"{key}|{isp_key}"
    digest = int.from_bytes(
        hashlib.blake2b(key.encode(), digest_size=8).digest(), "big"
    )
    return random.Random(digest)


def _isp_rng_key(isp_options: dict[str, str] | None) -> str:
    if not isp_options:
        return ""
    return (
        f"{isp_options.get('igp', '')}|"
        f"{isp_options.get('bgp_mode', '')}|"
        f"{isp_options.get('rpki', False)}|"
        f"{isp_options.get('backend', '')}|"
        f"{isp_options.get('device_profile', '')}"
    )


def _isp_protocol_kwargs(
    scenario: str, isp_options: dict[str, str] | None
) -> dict[str, object]:
    """Protocol kwargs for get_net_env_instance (topo comes from deploy_defaults)."""
    if not is_isp_base_topology(scenario) or not isp_options:
        return {}
    kwargs: dict[str, object] = {}
    for key in ("igp", "bgp_mode", "rpki"):
        if key in isp_options and isp_options[key] not in (None, "", "-"):
            kwargs[key] = isp_options[key]
    return kwargs


def _resolve_benchmark_backend(
    scenario: str, isp_options: dict[str, str] | None
) -> str:
    from nika.net_env.isp.profiles import DEFAULT_BACKEND_FOR_ISP
    from nika.net_env.net_env_pool import resolve_scenario_backend

    requested = None
    if isp_options:
        raw = isp_options.get("backend")
        if raw not in (None, "", "-"):
            requested = str(raw)
    return resolve_scenario_backend(
        scenario,
        backend=requested,
        default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP,
    )


def _isp_stack_kwargs(
    scenario: str, isp_options: dict[str, str] | None
) -> dict[str, object]:
    """Backend / device_profile kwargs for ISP (and other multi-backend) labs."""
    kwargs: dict[str, object] = {
        "backend": _resolve_benchmark_backend(scenario, isp_options),
    }
    if isp_options:
        profile = isp_options.get("device_profile")
        if profile not in (None, "", "-"):
            kwargs["device_profile"] = str(profile)
    return kwargs


def _get_net_env_for_benchmark(
    scenario: str,
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
):
    kwargs: dict = {}
    if topo_size and not is_isp_scenario(scenario):
        kwargs["topo_size"] = topo_size
    kwargs.update(_isp_protocol_kwargs(scenario, isp_options))
    kwargs.update(_isp_stack_kwargs(scenario, isp_options))
    return get_net_env_instance(scenario, **kwargs)


def _load_inventory(net_env) -> None:
    if net_env.lab is not None:
        net_env.load_machines()
        return

    spec = net_env.get_lab_spec()
    if spec is None:
        raise ValueError(f"Cannot derive benchmark inventory for {net_env.name!r}.")

    net_env.bmv2_switches = []
    net_env.ovs_switches = []
    net_env.sdn_controllers = []
    net_env.hosts = []
    net_env.routers = []
    net_env.switches = []
    net_env.servers = defaultdict(list)

    for node in spec.nodes:
        name = node.name
        kind = node.kind.lower()
        image = node.image.lower()
        if any(key in name for key in ("client", "pc", "host")) or kind == "linux":
            net_env.hosts.append(name)
        elif any(key in kind for key in ("srl", "ceos", "router")) or any(
            key in image for key in ("srl", "ceos", "frr")
        ):
            net_env.routers.append(name)
        else:
            net_env.switches.append(name)

    net_env.hosts = sorted(net_env.hosts)
    net_env.routers = sorted(net_env.routers)
    net_env.switches = sorted(net_env.switches)


def _benchmark_problem_class(problem: str) -> type[ProblemBase]:
    """Failure class owning ``problem``'s benchmark targets.

    Exact registry names only: aliases keep the ``ProblemBase`` default targets.
    """
    return list_avail_problem_instances().get(problem) or ProblemBase


def _resolve(
    problem: str,
    scenario: str,
    topo_size: str,
    *,
    seed: int,
    isp_options: dict[str, str] | None,
    net_env,
) -> tuple[dict[str, str], InjectTargetContext]:
    rng = _case_rng(
        seed,
        scenario,
        problem,
        topo_size,
        _isp_rng_key(isp_options),
    )
    if net_env is None:
        net_env = _get_net_env_for_benchmark(
            scenario, topo_size, isp_options=isp_options
        )
    _load_inventory(net_env)
    backend = _resolve_benchmark_backend(scenario, isp_options)

    hosts = net_env.hosts or []
    routers = net_env.routers or []
    servers = net_env.servers or {}
    bmv2 = net_env.bmv2_switches or []
    controllers = net_env.sdn_controllers or []

    pools = net_env.target_roles()
    host_pool = pools.get("hosts") or hosts
    router_pool = pools.get("routers") or routers

    host0 = _choice(rng, host_pool, _first(hosts) or "pc1")
    router0 = _choice(rng, router_pool, _first(routers) or host0)
    dns0 = _choice(rng, servers.get("dns"), host0)
    dhcp0 = _choice(rng, servers.get("dhcp"), dns0)
    web0 = _choice(rng, pools.get("web") or servers.get("web"), host0)
    lb0 = _choice(rng, servers.get("load_balancer"), web0)

    ctx = InjectTargetContext(
        problem=problem,
        scenario=scenario,
        topo_size=topo_size,
        seed=seed,
        isp_options=isp_options,
        net_env=net_env,
        rng=rng,
        backend=backend,
        roles=pools,
        hosts=hosts,
        routers=routers,
        switches=net_env.switches or [],
        servers=servers,
        bmv2=bmv2,
        controllers=controllers,
        host_pool=host_pool,
        router_pool=router_pool,
        host0=host0,
        router0=router0,
        dns0=dns0,
        dhcp0=dhcp0,
        web0=web0,
        lb0=lb0,
    )
    params = _benchmark_problem_class(problem).benchmark_inject_params(ctx)

    if is_isp_scenario(scenario):
        from nika.net_env.isp.inject_targets import enrich_isp_symptom_params

        inventory = getattr(net_env, "inventory", None)
        if not isinstance(inventory, dict):
            inventory = {}
        bgp_inv = inventory.get("bgp")
        enrich_isp_symptom_params(
            params,
            problem,
            inventory,
            bgp_inv if isinstance(bgp_inv, dict) else None,
        )

    return params, ctx


def resolve_inject_params(
    problem: str,
    scenario: str,
    topo_size: str = "",
    *,
    seed: int = DEFAULT_SEED,
    isp_options: dict[str, str] | None = None,
    net_env=None,
) -> dict[str, str]:
    """Return inject params for one benchmark row."""
    params, _ctx = _resolve(
        problem,
        scenario,
        topo_size,
        seed=seed,
        isp_options=isp_options,
        net_env=net_env,
    )
    return params


def validate_benchmark_case(
    scenario: str,
    problem: str,
    inject: dict[str, str],
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
    net_env=None,
) -> None:
    """Raise ValueError if a benchmark row is inconsistent with tags or topology."""
    from nika.net_env.net_env_pool import resolve_scenario_id, scenario_tags

    problems = list_avail_problem_instances()
    canonical = resolve_scenario_id(scenario)
    try:
        registered_tags = scenario_tags(canonical)
    except ValueError:
        raise ValueError(f"Unknown scenario {scenario!r}") from None
    if problem not in problems:
        raise ValueError(f"Unknown problem {problem!r}")

    problem_cls = problems[problem]
    if not problem_cls.is_compatible(canonical):
        compatible_columns = problem_cls.COMPATIBLE_COLUMNS
        raise ValueError(
            f"Incompatible {problem} on {scenario}: problem tags "
            f"{sorted(problem_cls.TAGS)}, scenario tags {sorted(registered_tags)}, "
            f"compatible columns "
            f"{sorted(compatible_columns) if compatible_columns is not None else 'any'}"
        )

    if net_env is None:
        net_env = _get_net_env_for_benchmark(
            canonical,
            topo_size,
            isp_options=isp_options,
        )
        _load_inventory(net_env)
    devices = _all_device_names(net_env)
    ifaces_by_device = _device_interfaces(net_env)

    for key in _DEVICE_KEYS:
        value = inject.get(key)
        if value and value not in devices:
            raise ValueError(
                f"Inject device {key}={value!r} not in {scenario} topology "
                f"(topo_size={topo_size!r}); known devices: {sorted(devices)}"
            )

    host_name = inject.get("host_name")
    intf_name = inject.get("intf_name")
    tunnel_iface = problem_cls.BENCHMARK_TUNNEL_IFACE
    # Tunnel ifaces are not Kathara L2 link endpoints; failures validate them.
    if host_name and intf_name and not tunnel_iface:
        device_ifaces = ifaces_by_device.get(host_name) or []
        if device_ifaces and intf_name not in device_ifaces:
            raise ValueError(
                f"Inject interface {intf_name!r} not on {host_name!r} in {scenario} "
                f"(topo_size={topo_size!r}); known interfaces: {device_ifaces}"
            )

    if (
        problem_cls.BENCHMARK_POINT_TO_POINT
        and host_name
        and intf_name
        and not tunnel_iface
    ):
        from nika.problems.rca.inventory import iter_link_termination_points

        needle = f"{host_name}:{intf_name}"
        matched = False
        for _key, tps in iter_link_termination_points(net_env):
            endpoints = [str(ep) for ep in tps]
            if needle not in endpoints:
                continue
            matched = True
            if len(endpoints) != 2:
                raise ValueError(
                    f"{problem} requires a point-to-point link at {needle} "
                    f"on {scenario} (topo_size={topo_size!r}); "
                    f"found {len(endpoints)} endpoints: {endpoints}"
                )
            break
        if not matched:
            raise ValueError(
                f"{problem} inject {needle} is not a link endpoint on {scenario} "
                f"(topo_size={topo_size!r})"
            )

    ctx = InjectValidationContext(
        scenario=scenario,
        canonical=canonical,
        topo_size=topo_size,
        isp_options=isp_options,
        net_env=net_env,
        devices=devices,
        ifaces_by_device=ifaces_by_device,
    )
    problem_cls.validate_benchmark_inject(ctx, inject)


_MULTI_FAULT_COORDINATORS: dict[frozenset[str], str] = {
    frozenset(
        {"mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"}
    ): "pmtud_blackhole",
    frozenset({"link_down", "host_missing_ip"}): "stacked_link_host",
    frozenset({"bgp_acl_block", "bgp_asn_misconfig"}): "bgp_acl_asn",
    frozenset({"dns_record_error", "host_incorrect_gateway"}): "dns_gateway",
}


def _coordinate_multi_inject_params(
    problems: list[str],
    params_by_problem: dict[str, dict[str, str]],
    *,
    scenario: str,
    net_env,
    rng: random.Random,
    backend: str,
) -> dict[str, dict[str, str]]:
    key = frozenset(problems)
    combo = _MULTI_FAULT_COORDINATORS.get(key)
    if combo == "pmtud_blackhole":
        mtu = dict(params_by_problem["mtu_mismatch"])
        frag = dict(params_by_problem["icmp_frag_needed_filter_misconfiguration"])
        frag["host_name"] = mtu["host_name"]
        params_by_problem["icmp_frag_needed_filter_misconfiguration"] = frag
        return params_by_problem

    if combo == "stacked_link_host":
        link = dict(params_by_problem["link_down"])
        host = dict(params_by_problem["host_missing_ip"])
        if link.get("host_name") == host.get("host_name"):
            web_hosts = list((getattr(net_env, "servers", None) or {}).get("web") or [])
            if web_hosts:
                host["host_name"] = web_hosts[0]
                ifaces = _device_interfaces(net_env).get(web_hosts[0]) or ["eth0"]
                host["intf_name"] = ifaces[0]
            else:
                hosts = list(net_env.hosts or [])
                alt = next((h for h in hosts if h != link.get("host_name")), None)
                if alt is not None:
                    host["host_name"] = alt
                    ifaces = _device_interfaces(net_env).get(alt) or ["eth0"]
                    host["intf_name"] = ifaces[0]
        params_by_problem["host_missing_ip"] = host
        return params_by_problem

    if combo == "bgp_acl_asn":
        acl = dict(params_by_problem["bgp_acl_block"])
        asn = dict(params_by_problem["bgp_asn_misconfig"])
        routers = list(net_env.routers or [])
        if acl.get("host_name") == asn.get("host_name") and len(routers) >= 2:
            asn["host_name"] = (
                routers[1] if routers[0] == acl.get("host_name") else routers[0]
            )
        params_by_problem["bgp_asn_misconfig"] = asn
        return params_by_problem

    if combo == "dns_gateway":
        dns = dict(params_by_problem["dns_record_error"])
        gw = dict(params_by_problem["host_incorrect_gateway"])
        if scenario == "campus_lan":
            dns["host_name"] = "dns_server"
            gw["host_name"] = "pc_1_1_1_1"
        elif dns.get("host_name") == gw.get("host_name"):
            hosts = list(net_env.hosts or [])
            corp = [h for h in hosts if h.endswith("_corp_pc")]
            alt = next((h for h in corp if h != dns.get("host_name")), None)
            if alt is None:
                alt = next((h for h in hosts if h != dns.get("host_name")), None)
            if alt is not None:
                gw["host_name"] = alt
        params_by_problem["dns_record_error"] = dns
        params_by_problem["host_incorrect_gateway"] = gw
        return params_by_problem

    return params_by_problem


def resolve_multi_inject_params(
    problems: list[str],
    scenario: str,
    topo_size: str = "",
    *,
    seed: int = DEFAULT_SEED,
    isp_options: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Return per-problem inject params for a coordinated multi-fault case."""
    if len(problems) < 2:
        raise ValueError("resolve_multi_inject_params requires at least two problems.")
    resolved: dict[str, dict[str, str]] = {}
    for problem in problems:
        resolved[problem] = resolve_inject_params(
            problem,
            scenario,
            topo_size,
            seed=seed,
            isp_options=isp_options,
        )
    net_env = _get_net_env_for_benchmark(scenario, topo_size, isp_options=isp_options)
    if hasattr(net_env, "load_machines") and not getattr(net_env, "hosts", None):
        net_env.load_machines()
    rng = _case_rng(
        seed,
        scenario,
        "+".join(sorted(problems)),
        topo_size,
        _isp_rng_key(isp_options),
    )
    backend = _resolve_benchmark_backend(scenario, isp_options)
    return _coordinate_multi_inject_params(
        problems,
        resolved,
        scenario=scenario,
        net_env=net_env,
        rng=rng,
        backend=backend,
    )
