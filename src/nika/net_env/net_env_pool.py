"""Registered network environment scenarios (metadata + lazy class load)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import inspect
from pathlib import Path
from typing import Any

from nika.net_env.base import NetworkEnvBase
from nika.runtime.extras import raise_missing_extra, require_backend_extra


@dataclass(frozen=True)
class NetEnvSpec:
    """Import-safe scenario metadata (no lab-backend packages required)."""

    lab_name: str
    module: str
    class_name: str
    tags: tuple[str, ...]
    supported_backends: tuple[str, ...]
    topo_size: Any = None

    @property
    def LAB_NAME(self) -> str:
        return self.lab_name

    @property
    def TAGS(self) -> list[str]:
        return list(self.tags)

    @property
    def SUPPORTED_BACKENDS(self) -> list[str]:
        return list(self.supported_backends)

    @property
    def TOPO_SIZE(self) -> Any:
        return self.topo_size


_NET_ENV_SPECS: dict[str, NetEnvSpec] = {
    "dc_clos_bgp": NetEnvSpec(
        lab_name="dc_clos_bgp",
        module="nika.net_env.kathara.data_center_routing.dc_clos_bgp.lab_workers",
        class_name="DCClosBGP",
        tags=("arp", "link", "mac", "bgp", "icmp", "frr", "pc"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "dc_clos_service": NetEnvSpec(
        lab_name="dc_clos_service",
        module="nika.net_env.kathara.data_center_routing.dc_clos_bgp.lab_services",
        class_name="DCClosService",
        tags=("arp", "link", "mac", "bgp", "icmp", "frr", "dns", "pc", "http"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "ospf_enterprise_dhcp": NetEnvSpec(
        lab_name="ospf_enterprise_dhcp",
        module="nika.net_env.kathara.intradomain_routing.ospf_enterprise.lab_dhcp",
        class_name="OSPFEnterpriseDHCP",
        tags=(
            "arp",
            "link",
            "web",
            "icmp",
            "frr",
            "dns",
            "ospf",
            "dhcp",
            "pc",
            "mac",
            "http",
            "load_balancer",
        ),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "ospf_enterprise_static": NetEnvSpec(
        lab_name="ospf_enterprise_static",
        module="nika.net_env.kathara.intradomain_routing.ospf_enterprise.lab_static",
        class_name="OSPFEnterpriseStatic",
        tags=("pc", "ospf", "mac", "http", "link", "frr", "icmp", "arp"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "rip_small_internet_vpn": NetEnvSpec(
        lab_name="rip_small_internet_vpn",
        module="nika.net_env.kathara.intradomain_routing.rip_vpn.lab",
        class_name="RIPSmallInternetVPN",
        tags=("link", "http", "pc", "frr", "mac", "arp", "vpn", "icmp"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "sdn_star": NetEnvSpec(
        lab_name="sdn_star",
        module="nika.net_env.kathara.sdn.star_topo",
        class_name="SDNStar",
        tags=("link", "sdn", "pc", "mac", "arp", "icmp"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "sdn_clos": NetEnvSpec(
        lab_name="sdn_clos",
        module="nika.net_env.kathara.sdn.clos_topo",
        class_name="SDNClos",
        tags=("link", "sdn", "pc", "mac", "arp", "icmp"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "p4_bloom_filter": NetEnvSpec(
        lab_name="p4_bloom_filter",
        module="nika.net_env.kathara.p4.p4_bloom_filter.lab",
        class_name="P4BloomFilter",
        tags=("link", "pc", "p4", "mac", "arp", "icmp", "bloom_filter"),
        supported_backends=("kathara",),
    ),
    "p4_counter": NetEnvSpec(
        lab_name="p4_counter",
        module="nika.net_env.kathara.p4.p4_counter.lab",
        class_name="P4Counter",
        tags=("link", "pc", "p4", "mac", "arp", "icmp"),
        supported_backends=("kathara",),
    ),
    "p4_int": NetEnvSpec(
        lab_name="p4_int",
        module="nika.net_env.kathara.p4.p4_int.lab",
        class_name="P4INT",
        tags=("link", "pc", "p4", "mac", "arp", "icmp", "int"),
        supported_backends=("kathara",),
    ),
    "p4_mpls": NetEnvSpec(
        lab_name="p4_mpls",
        module="nika.net_env.kathara.p4.p4_mpls.lab",
        class_name="P4_MPLS",
        tags=("link", "pc", "p4", "mac", "arp", "icmp", "mpls"),
        supported_backends=("kathara",),
    ),
    "simple_bgp": NetEnvSpec(
        lab_name="simple_bgp",
        module="nika.net_env.kathara.interdomain_routing.simple_bgp.lab",
        class_name="SimpleBGP",
        tags=("arp", "link", "mac", "bgp", "icmp", "frr", "pc"),
        supported_backends=("kathara",),
    ),
    "min3clos": NetEnvSpec(
        lab_name="min3clos",
        module="nika.net_env.containerlab.min3clos.lab",
        class_name="ContainerlabMin3Clos",
        tags=("clos", "srl", "bgp", "link", "containerlab", "fabric"),
        supported_backends=("containerlab",),
        topo_size=5,
    ),
    "k8s_lab": NetEnvSpec(
        lab_name="k8s_lab",
        module="nika.net_env.kathara.kubernetes.k8s_lab.lab",
        class_name="K8sFatTreeBGP",
        tags=(
            "kubernetes",
            "k3s",
            "k8s_control_plane",
            "k8s_workload",
            "ingress",
            "metallb",
            "coredns",
            "kube_proxy",
            "k8s_storage",
            "fat-tree",
            "bgp",
            "frr",
            "link",
            "pc",
            "icmp",
            "arp",
            "mac",
        ),
        supported_backends=("kathara",),
    ),
    "llmd_lab": NetEnvSpec(
        lab_name="llmd_lab",
        module="nika.net_env.kathara.kubernetes.llmd_lab.lab",
        class_name="LLMDInferenceCluster",
        tags=(
            "kubernetes",
            "k3s",
            "k8s_control_plane",
            "metallb",
            "coredns",
            "kube_proxy",
            "llm",
            "inference",
            "link",
            "pc",
            "http",
            "icmp",
            "arp",
            "mac",
        ),
        supported_backends=("kathara",),
    ),
}

_CLASS_CACHE: dict[str, type[NetworkEnvBase]] = {}


def _require_scenario(scenario_name: str) -> NetEnvSpec:
    if scenario_name not in _NET_ENV_SPECS:
        raise ValueError(
            f"Network environment '{scenario_name}' not found in the pool."
        )
    return _NET_ENV_SPECS[scenario_name]


def _load_net_env_class(scenario_name: str) -> type[NetworkEnvBase]:
    if scenario_name in _CLASS_CACHE:
        return _CLASS_CACHE[scenario_name]
    spec = _require_scenario(scenario_name)
    backend = spec.supported_backends[0] if len(spec.supported_backends) == 1 else None
    if backend:
        require_backend_extra(backend)
    try:
        module = import_module(spec.module)
        cls = getattr(module, spec.class_name)
    except ImportError as exc:
        extra = backend or "labs"
        raise_missing_extra(extra, cause=exc)
    _CLASS_CACHE[scenario_name] = cls
    return cls


def scenario_tags(scenario_name: str) -> list[str]:
    """Return metadata tags declared by the network environment."""
    return list(_require_scenario(scenario_name).tags)


def scenario_supported_backends(scenario_name: str) -> list[str]:
    """Return backends supported by ``scenario_name``."""
    return list(_require_scenario(scenario_name).supported_backends)


def scenario_backend(scenario_name: str) -> str:
    """Return the lab backend bound to ``scenario_name``."""
    supported = scenario_supported_backends(scenario_name)
    if len(supported) != 1:
        raise ValueError(
            f"Scenario '{scenario_name}' must declare exactly one backend; "
            f"found: {', '.join(supported)}"
        )
    return supported[0]


def get_net_env_instance(
    scenario_name: str, *, backend: str = "kathara", **kwargs
) -> NetworkEnvBase:
    """Get an instance of the specified network environment.

    Args:
        scenario_name: The name of the network environment.
        backend: Lab runtime backend (``kathara`` or ``containerlab``).

    Returns:
        An instance of the specified network environment.

    Raises:
        ValueError: If the specified network environment is not found or backend unsupported.
    """
    spec = _require_scenario(scenario_name)
    if backend not in spec.supported_backends:
        raise ValueError(
            f"Scenario '{scenario_name}' does not support backend '{backend}'. "
            f"Supported: {', '.join(spec.supported_backends)}"
        )
    cls = _load_net_env_class(scenario_name)
    lab_name = kwargs.pop("lab_name", None)
    topology_file = kwargs.pop("topology_file", None)
    runtime_workdir = kwargs.pop("runtime_workdir", None)
    # Many Kathara lab ``__init__`` signatures omit ``backend`` (and ``**kwargs``).
    # Pass it only when accepted; always assign afterward so ``instance.backend``
    # matches the resolved runtime backend.
    init_params = inspect.signature(cls.__init__).parameters
    accepts_backend = "backend" in init_params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in init_params.values()
    )
    instance = cls(backend=backend, **kwargs) if accepts_backend else cls(**kwargs)
    instance.backend = backend
    if lab_name:
        instance.name = lab_name
        if instance.lab is not None:
            instance.lab.name = lab_name
    if topology_file is not None:
        instance.topology_file = Path(topology_file)
    if runtime_workdir is not None:
        instance.runtime_workdir = Path(runtime_workdir)
    return instance


def list_all_net_envs(*, backend: str | None = None) -> dict[str, NetEnvSpec]:
    """List available network environment specs, optionally filtered by backend."""
    if backend is None:
        return dict(_NET_ENV_SPECS)
    return {
        name: spec
        for name, spec in _NET_ENV_SPECS.items()
        if backend in spec.supported_backends
    }


def scenario_requires_topo_size(scenario_name: str) -> bool:
    """Return True if this scenario's lab expects an explicit topo size (s/m/l)."""
    topo_size = _require_scenario(scenario_name).topo_size
    return isinstance(topo_size, list)


def scenario_source_path(scenario_name: str) -> Path:
    """Return the scenario module file path without importing lab backends."""
    import importlib.util

    spec = _require_scenario(scenario_name)
    module_spec = importlib.util.find_spec(spec.module)
    if module_spec is None or module_spec.origin is None:
        raise ValueError(
            f"Cannot resolve source path for network environment '{scenario_name}'."
        )
    return Path(module_spec.origin).resolve()
