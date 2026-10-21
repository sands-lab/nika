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
    """Native configuration per converted router, and original router commands
    the platform does not reproduce (reported, never silently dropped)."""

    configs: dict[str, str]
    unreproduced: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class NativeRouterPlatform:
    """How a vendor module converts declared FRR routers and admits the lab."""

    convert: Callable[[object, Sequence[str]], NativeConversion]
    # Raises before any container is created (image, memory admission).
    check_deployable: Callable[[object], None]
    verify_max_wait_sec: int


# Vendor modules exposing ``PLATFORM``; add a profile here to support it.
_PLATFORMS = {"iosxr": "nika.net_env.utils.iosxr.platform"}


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
    native_router_unreproduced: Mapping[str, tuple[str, ...]] = MappingProxyType({})

    def router_roles(self) -> Mapping[str, Sequence[str]]:
        """Return router names by role for profile eligibility."""
        raise NotImplementedError

    def apply_device_profile(
        self, profile: str | None, *, routing_parameters=None
    ) -> None:
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
        if routing_parameters is not None:
            raise ValueError("Native router profiles do not bind solved parameters")
        platform = native_platform(profile)
        names = self._eligible(roles)
        conversion = platform.convert(self, names)
        self.deployment_configs = conversion.configs
        self.native_router_unreproduced = conversion.unreproduced
        if conversion.unreproduced:
            from nika.utils.logger import system_logger

            system_logger.warning(
                f"{self.LAB_NAME} on {profile}: commands not reproduced by native "
                f"routers: {dict(conversion.unreproduced)}"
            )
            self.metadata["native_router_unreproduced"] = {
                name: list(commands)
                for name, commands in conversion.unreproduced.items()
            }
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
