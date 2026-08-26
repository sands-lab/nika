"""Registered network environment scenarios (metadata + lazy class load)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import inspect
from pathlib import Path
from typing import Any, Mapping

from nika.net_env.base import NetworkEnvBase
from nika.utils.dependencies import raise_missing_extra, require_backend_extra


@dataclass(frozen=True)
class BackendEnvBinding:
    """Module/class binding for one lab backend of a scenario."""

    module: str
    class_name: str


@dataclass(frozen=True)
class NetEnvSpec:
    """Import-safe scenario metadata (no lab-backend packages required)."""

    lab_name: str
    module: str
    class_name: str
    tags: tuple[str, ...]
    supported_backends: tuple[str, ...]
    topo_size: Any = None
    # Optional per-backend overrides; when absent, ``module``/``class_name`` apply
    # to every supported backend (single-binding scenarios).
    backend_bindings: Mapping[str, BackendEnvBinding] | None = None

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

    def binding_for(self, backend: str) -> BackendEnvBinding:
        if backend not in self.supported_backends:
            raise ValueError(
                f"Scenario '{self.lab_name}' does not support backend '{backend}'. "
                f"Supported: {', '.join(self.supported_backends)}"
            )
        if self.backend_bindings and backend in self.backend_bindings:
            return self.backend_bindings[backend]
        return BackendEnvBinding(module=self.module, class_name=self.class_name)


DC_CLOS_SCENARIO = "dc_clos"
CAMPUS_LAN_SCENARIO = "campus_lan"
ENTERPRISE_BRANCH_SCENARIO = "enterprise_branch"
SDN_L3_CLOS_SCENARIO = "sdn_l3_clos"
P4_DC_FABRIC_SCENARIO = "p4_dc_fabric"
P4_DC_GATEWAY_SCENARIO = "p4_dc_gateway"

_NET_ENV_SPECS: dict[str, NetEnvSpec] = {
    "dc_clos": NetEnvSpec(
        lab_name="dc_clos",
        module="nika.net_env.dc_clos.lab",
        class_name="DCClos",
        tags=(
            "arp",
            "link",
            "mac",
            "bgp",
            "icmp",
            "frr",
            "pc",
            "dns",
            "http",
            "dc_clos",
            "forwarding_device",
        ),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "campus_lan": NetEnvSpec(
        lab_name="campus_lan",
        module="nika.net_env.campus_lan.lab",
        class_name="CampusLan",
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
            "forwarding_device",
        ),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "enterprise_branch": NetEnvSpec(
        lab_name="enterprise_branch",
        module="nika.net_env.enterprise_branch.lab",
        class_name="EnterpriseBranch",
        tags=(
            "arp",
            "link",
            "mac",
            "icmp",
            "frr",
            "bgp",
            "pc",
            "http",
            "vpn",
            "nat",
            "forwarding_device",
        ),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "sdn_l3_clos": NetEnvSpec(
        lab_name="sdn_l3_clos",
        module="nika.net_env.sdn_l3_clos.l3_clos_topo",
        class_name="SDNL3Clos",
        tags=("link", "sdn", "pc", "mac", "arp", "icmp", "http", "forwarding_device"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "p4_dc_fabric": NetEnvSpec(
        lab_name="p4_dc_fabric",
        module="nika.net_env.p4_dc_fabric.lab",
        class_name="P4DcFabric",
        tags=("link", "pc", "p4", "p4_runtime", "mac", "arp", "icmp", "http"),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "p4_dc_gateway": NetEnvSpec(
        lab_name="p4_dc_gateway",
        module="nika.net_env.p4_dc_gateway.lab",
        class_name="P4DcGateway",
        tags=(
            "link",
            "pc",
            "p4",
            "p4_runtime",
            "mac",
            "arp",
            "icmp",
            "http",
            "int",
            "telemetry",
            "flow_tracking",
            "ecn",
            "queue",
            "l4_load_balancer",
        ),
        supported_backends=("kathara",),
        topo_size=["s", "m", "l"],
    ),
    "iosxr_simple_bgp": NetEnvSpec(
        lab_name="iosxr_simple_bgp",
        module="nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.lab",
        class_name="IosXrSimpleBGP",
        tags=("arp", "link", "bgp", "icmp", "iosxr", "pc"),
        supported_backends=("kathara",),
    ),
    "isp": NetEnvSpec(
        lab_name="isp",
        module="nika.net_env.isp.kathara.lab",
        class_name="Isp",
        tags=(
            "isp",
            "sndlib",
            "frr",
            "isis",
            "ospf",
            "bgp",
            "rpki",
            "igp",
            "link",
            "icmp",
            "srl",
            "containerlab",
        ),
        supported_backends=("kathara", "containerlab"),
        topo_size=["s", "m", "l"],
        backend_bindings={
            "kathara": BackendEnvBinding(
                module="nika.net_env.isp.kathara.lab",
                class_name="Isp",
            ),
            "containerlab": BackendEnvBinding(
                module="nika.net_env.isp.containerlab.lab",
                class_name="Isp",
            ),
        },
    ),
    "min3clos": NetEnvSpec(
        lab_name="min3clos",
        module="nika.net_env.min3clos.lab",
        class_name="ContainerlabMin3Clos",
        tags=("clos", "srl", "bgp", "link", "containerlab", "fabric"),
        supported_backends=("containerlab",),
        topo_size=5,
    ),
    "k8s_lab": NetEnvSpec(
        lab_name="k8s_lab",
        module="nika.net_env.k8s_lab.lab",
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
            "network_policy",
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
        module="nika.net_env.llmd_lab.lab",
        class_name="LLMDInferenceCluster",
        tags=(
            "kubernetes",
            "k3s",
            "k8s_control_plane",
            "metallb",
            "coredns",
            "kube_proxy",
            "network_policy",
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

_CLASS_CACHE: dict[tuple[str, str], type[NetworkEnvBase]] = {}


def resolve_scenario_id(scenario_name: str) -> str:
    """Validate and return a registered canonical scenario ID."""
    if scenario_name in _NET_ENV_SPECS:
        return scenario_name
    raise ValueError(f"Network environment '{scenario_name}' not found in the pool.")


def is_dc_clos_scenario(scenario_name: str) -> bool:
    return resolve_scenario_id(scenario_name) == DC_CLOS_SCENARIO


def is_campus_lan_scenario(scenario_name: str) -> bool:
    return resolve_scenario_id(scenario_name) == CAMPUS_LAN_SCENARIO


def is_enterprise_branch_scenario(scenario_name: str) -> bool:
    return resolve_scenario_id(scenario_name) == ENTERPRISE_BRANCH_SCENARIO


def _require_scenario(scenario_name: str) -> NetEnvSpec:
    return _NET_ENV_SPECS[resolve_scenario_id(scenario_name)]


def _load_net_env_class(scenario_name: str, *, backend: str) -> type[NetworkEnvBase]:
    canonical = resolve_scenario_id(scenario_name)
    cache_key = (canonical, backend)
    if cache_key in _CLASS_CACHE:
        return _CLASS_CACHE[cache_key]
    spec = _require_scenario(canonical)
    binding = spec.binding_for(backend)
    require_backend_extra(backend)
    try:
        module = import_module(binding.module)
        cls = getattr(module, binding.class_name)
    except ImportError as exc:
        raise_missing_extra(backend, cause=exc)
    _CLASS_CACHE[cache_key] = cls
    return cls


def scenario_tags(scenario_name: str) -> list[str]:
    """Return metadata tags declared by the network environment."""
    return list(_require_scenario(scenario_name).tags)


# Deploy variants shown as coverage-matrix columns for ``isp``.
ISP_COVERAGE_CONFIGS: tuple[str, ...] = (
    "isis",
    "ospf",
    "ibgp_rr",
    "abilene-ebgp",
    "abilene-ebgp-rpki",
    "geant-ebgp-rpki",
)

_ISP_COVERAGE_BASE_TAGS: frozenset[str] = frozenset(
    {"isp", "sndlib", "frr", "igp", "link", "icmp"}
)


def parse_column(column: str) -> tuple[str, str | None]:
    """Return ``(scenario, config)`` for a coverage column id."""
    if "/" in column:
        scenario, _, config = column.partition("/")
        return scenario, config
    return column, None


def coverage_columns() -> list[str]:
    """Stable ordered list of coverage-matrix column ids."""
    columns: list[str] = []
    for name in sorted(list_all_net_envs()):
        if name == "isp":
            columns.extend(f"isp/{cfg}" for cfg in ISP_COVERAGE_CONFIGS)
        else:
            columns.append(name)
    return columns


def effective_tags(column: str) -> frozenset[str]:
    """Tags exposed by one deployed scenario config (not class-level unions)."""
    scenario, config = parse_column(column)
    if scenario == "isp":
        if config == "isis":
            return _ISP_COVERAGE_BASE_TAGS | frozenset({"isis"})
        if config == "ospf":
            return _ISP_COVERAGE_BASE_TAGS | frozenset({"ospf"})
        if config == "ibgp_rr":
            return _ISP_COVERAGE_BASE_TAGS | frozenset({"isis", "bgp"})
        if config == "abilene-ebgp":
            return _ISP_COVERAGE_BASE_TAGS | frozenset({"ospf", "bgp"})
        if config == "abilene-ebgp-rpki":
            return _ISP_COVERAGE_BASE_TAGS | frozenset({"ospf", "bgp", "rpki"})
        if config == "geant-ebgp-rpki":
            return _ISP_COVERAGE_BASE_TAGS | frozenset({"ospf", "bgp", "rpki"})
        raise ValueError(f"Unknown isp config {config!r}")
    return frozenset(scenario_tags(scenario))


def scenario_supported_backends(scenario_name: str) -> list[str]:
    """Return backends supported by ``scenario_name``."""
    return list(_require_scenario(scenario_name).supported_backends)


def resolve_scenario_backend(
    scenario_name: str,
    *,
    backend: str | None = None,
    default_when_ambiguous: str | None = None,
) -> str:
    """Resolve which lab backend to use for ``scenario_name``.

    - Explicit ``backend`` must be in the scenario's supported list.
    - Single-backend scenarios resolve without an explicit choice.
    - Multi-backend scenarios require ``backend``, or ``default_when_ambiguous``
      when that default is supported.
    """
    supported = scenario_supported_backends(scenario_name)
    if backend is not None:
        if backend not in supported:
            raise ValueError(
                f"Scenario '{scenario_name}' does not support backend '{backend}'. "
                f"Supported: {', '.join(supported)}"
            )
        return backend
    if len(supported) == 1:
        return supported[0]
    if default_when_ambiguous is not None and default_when_ambiguous in supported:
        return default_when_ambiguous
    raise ValueError(
        f"Scenario '{scenario_name}' supports multiple backends "
        f"({', '.join(supported)}); pass --backend."
    )


def scenario_backend(scenario_name: str) -> str:
    """Return the sole backend for a single-backend scenario.

    Multi-backend scenarios must use :func:`resolve_scenario_backend` with an
    explicit ``backend`` (or ``default_when_ambiguous``).
    """
    return resolve_scenario_backend(scenario_name)


def get_net_env_instance(
    scenario_name: str, *, backend: str = "kathara", **kwargs
) -> NetworkEnvBase:
    """Get an instance of the specified network environment.

    Args:
        scenario_name: A registered canonical scenario ID.
        backend: Lab runtime backend (``kathara`` or ``containerlab``).

    Returns:
        An instance of the specified network environment.

    Raises:
        ValueError: If the specified network environment is not found or backend unsupported.
    """
    canonical = resolve_scenario_id(scenario_name)
    resolved = resolve_scenario_backend(canonical, backend=backend)
    cls = _load_net_env_class(canonical, backend=resolved)
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
    instance = cls(backend=resolved, **kwargs) if accepts_backend else cls(**kwargs)
    instance.backend = resolved
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
