"""Per-node native router profiles for Kathara Ethernet environments.

An environment groups its routers into roles and declares which roles may run
each native profile. Selecting a profile with ``device_profile`` converts only
the routers in those roles; bridging gateways, tunnel edges, hosts and services
keep their FRR/Linux design, so topology, tasks and ground truth are unchanged.
Without a native profile the environment is built exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType
from typing import ClassVar


@dataclass(frozen=True)
class NativeConversion:
    """Native configuration per converted router, plus Linux commands (such as
    egress shaping) that the native image cannot run itself."""

    configs: dict[str, str]
    netns_commands: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class NativeInterface:
    """A native router interface under its Kathara (Linux) name."""

    name: str
    address: str
    ospf_area: str | None = None


@dataclass(frozen=True)
class NativeRouterNode:
    device_name: str
    interfaces: tuple[NativeInterface, ...]
    loopback: str | None = None


@dataclass(frozen=True)
class NativeRouterPlan:
    """Concrete native routers, as the vendor runtime verifiers address them."""

    nodes: tuple[NativeRouterNode, ...]


@dataclass(frozen=True)
class NativeRouterValidation:
    """Vendor projection used by specification-driven validation."""

    # Linux interface name -> native interface name.
    interface: Callable[[str], str]
    # (config, native interfaces shut down) -> Batfish snapshot file content.
    batfish_config: Callable[[str, Sequence[str]], str]
    # (node, config) -> concrete interfaces for the runtime verifier.
    node: Callable[[str, str], NativeRouterNode]
    # (runtime, contract, plan) -> ValidationReport for native observations.
    verify_contract: Callable[..., object]
    # Shell command that applies configuration lines (runtime negative controls).
    config_command: Callable[[Sequence[str]], str]
    route_inspection_command: str
    # Where a deployable native configuration is installed on the Kathara
    # machine, and the netlab device that generates it (if any).
    config_file: str | None = None
    netlab_device: str | None = None
    # (router, FRR configuration, Linux startup) -> native statements NIKA
    # appends to netlab output.
    netlab_retained: Callable[[str, str, str], str] | None = None


@dataclass(frozen=True)
class NativeRouterPlatform:
    """How a vendor module converts declared FRR routers and admits the lab."""

    convert: Callable[[object, Sequence[str]], NativeConversion]
    # Raises before any container is created (image, memory admission).
    check_deployable: Callable[[object], None]
    verify_max_wait_sec: int
    validation: NativeRouterValidation | None = None
    # (runtime, node) -> provider of failure operations; see
    # ``nika.problems.support.router_ops``. None when no failure is supported.
    fault_ops: Callable[[object, str], object] | None = None


# Vendor modules exposing ``PLATFORM``; add a profile here to support it.
_PLATFORMS = {"iosxr": "nika.net_env.utils.iosxr.platform"}


def native_profiles() -> frozenset[str]:
    """Return the device profiles that run native router images."""
    return frozenset(_PLATFORMS)


def native_platform(profile: str) -> NativeRouterPlatform:
    if profile not in _PLATFORMS:
        raise ValueError(f"No native router platform for device_profile={profile!r}")
    return import_module(_PLATFORMS[profile]).PLATFORM


class NativeRouterEnv:
    """Mixin for environments whose routers may run native router profiles."""

    # profile -> roles whose routers that profile may replace
    NATIVE_PROFILE_ROLES: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType({})
    # profile -> supported topology sizes (all sizes when absent)
    NATIVE_PROFILE_SIZES: ClassVar[Mapping[str, tuple[str, ...]]] = MappingProxyType({})
    device_profile: str = "frr"
    native_routers: Mapping[str, str] = MappingProxyType({})
    native_netns_commands: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    # router -> (FRR configuration, Linux startup) it was converted from
    native_sources: Mapping[str, tuple[str, str]] = MappingProxyType({})

    def router_roles(self) -> Mapping[str, Sequence[str]]:
        """Return router names by role for profile eligibility."""
        raise NotImplementedError

    def apply_device_profile(self, profile: str | None) -> None:
        """Convert the routers of the eligible roles to ``profile``."""
        if profile in (None, "frr"):
            return
        roles = self.NATIVE_PROFILE_ROLES.get(profile)
        if not roles:
            raise ValueError(
                f"{self.LAB_NAME} does not support device_profile={profile!r}"
            )
        sizes = self.NATIVE_PROFILE_SIZES.get(profile)
        size = getattr(self, "topo_size", None)
        if sizes and size not in sizes:
            raise ValueError(
                f"{self.LAB_NAME} with device_profile={profile} supports size "
                f"{', '.join(sizes)} only; size {size!r} has "
                f"{len(self._eligible(roles))} eligible routers."
            )
        # Solved parameters are already bound into the FRR sketch; the
        # platform translates them with the rest of each router's design.
        platform = native_platform(profile)
        names = self._eligible(roles)
        # Keep each converted router's FRR design: completion, configuration
        # generators and comparisons read the same sketch as the FRR deployment.
        self.native_sources = MappingProxyType(
            {
                name: (
                    self.lab.machines[name].fs.readtext("etc/frr/frr.conf"),
                    self.lab.fs.readtext(f"{name}.startup"),
                )
                for name in names
            }
        )
        conversion = platform.convert(self, names)
        self.deployment_configs = conversion.configs
        self.native_netns_commands = MappingProxyType(conversion.netns_commands)
        self.device_profile = profile
        self.native_routers = MappingProxyType(dict.fromkeys(names, profile))
        # Native routers boot and apply configuration in minutes.
        self.VERIFY_MAX_WAIT_SEC = max(
            getattr(self, "VERIFY_MAX_WAIT_SEC", 0), platform.verify_max_wait_sec
        )
        self.VERIFY_RETRY_DELAY_SEC = 10
        self.desc = (
            f"{self.desc} Routers in roles {', '.join(sorted(roles))} run "
            f"{profile}; other nodes run FRR/Linux."
        )

    def _eligible(self, roles) -> tuple[str, ...]:
        by_role = self.router_roles()
        return tuple(sorted({name for role in roles for name in by_role.get(role, ())}))

    def deploy(self):
        if self.native_routers:
            native_platform(self.device_profile).check_deployable(self)
        super().deploy()
        for node, commands in self.native_netns_commands.items():
            run_in_network_namespace(self, node, commands)


def require_native_validation(env) -> None:
    """Reject native routers whose platform has no validation projection."""
    for name, profile in getattr(env, "native_routers", {}).items():
        if native_platform(profile).validation is None:
            raise ValueError(
                f"No mixed-vendor validation projection for {profile} router {name}"
            )


def router_sketches(env) -> dict[str, tuple[str, str]]:
    """Return each router's FRR design and startup, including converted routers."""

    def startup(name):
        path = f"{name}.startup"
        return env.lab.fs.readtext(path) if env.lab.fs.exists(path) else ""

    sketches = {
        name: (machine.fs.readtext("etc/frr/frr.conf"), startup(name))
        for name, machine in env.lab.machines.items()
        if machine.fs and machine.fs.exists("etc/frr/frr.conf")
    }
    sketches.update(getattr(env, "native_sources", {}))
    return sketches


def native_router_plan(env) -> NativeRouterPlan:
    """Describe the environment's native routers from their concrete configs."""
    nodes = []
    for name, profile in sorted(env.native_routers.items()):
        validation = native_platform(profile).validation
        nodes.append(validation.node(name, env.deployment_configs[name]))
    return NativeRouterPlan(tuple(nodes))


def verify_native_router_contract(env, fallback):
    """Return a runtime verifier that observes each intent on its own platform.

    Intents observed on a native router (the adjacency's local node, the
    route's node or the probe source) use that vendor's verifier; all others
    use ``fallback``, the environment's FRR/Linux verifier. Results keep the
    contract's order, so reports compare across vendors.
    """
    from nika.net_env.contract import ValidationReport

    routers = native_router_plan(env)

    def observer(intent):
        for candidate in (intent.adjacency, intent.route_next_hops, intent.bgp_route):
            if candidate is not None:
                return getattr(candidate, "local_node", None) or candidate.node
        return intent.source.name if intent.source is not None else None

    def verify(runtime, *, contract, plan=None):
        groups = {}
        for intent in contract.intents:
            profile = env.native_routers.get(observer(intent))
            groups.setdefault(profile, []).append(intent)
        rows = {}
        for profile, intents in groups.items():
            part = contract.model_copy(update={"intents": tuple(intents)})
            if profile is None:
                report = fallback(runtime, contract=part, plan=None)
            else:
                report = native_platform(profile).validation.verify_contract(
                    runtime, part, routers
                )
            rows.update({row.intent: row for row in report.results})
        name = "+".join(sorted(p or "frr" for p in groups)) + "-runtime"
        # Each row keeps the platform verifier that observed it as evidence.
        return ValidationReport.from_results(
            contract,
            name,
            [
                rows[intent.id].model_copy(
                    update={
                        "verifier": name,
                        "evidence": {
                            **rows[intent.id].evidence,
                            "platform_verifier": rows[intent.id].verifier,
                        },
                    }
                )
                for intent in contract.intents
            ],
        )

    return verify


def run_in_network_namespace(env, node: str, commands: Sequence[str]) -> None:
    """Run Linux commands in a Kathara node's network namespace.

    A short-lived NIKA helper container joins the node's namespace, so images
    without these tools (for example ``tc`` in XRd) keep their link setup.
    """
    from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
    from nika.runtime.shared.containers import docker_client

    container = env._build_runtime().get_container(node)
    docker_client().containers.run(
        nika_image("base"),
        ["/bin/sh", "-ec", "\n".join(commands)],
        network_mode=f"container:{container.id}",
        cap_add=["NET_ADMIN"],
        remove=True,
    )
