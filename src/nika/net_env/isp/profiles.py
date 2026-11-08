"""ISP device-profile / backend pairing for multi-NOS labs."""

from __future__ import annotations

from typing import Literal

IspDeviceProfile = Literal["frr", "nokia_srlinux", "sonic", "iosxr", "routeros"]

SUPPORTED_DEVICE_PROFILES: tuple[IspDeviceProfile, ...] = (
    "frr",
    "nokia_srlinux",
    "sonic",
    "iosxr",
    "routeros",
)

# Allowed runtime/backend combinations; protocol support is checked by the renderer.
SUPPORTED_BACKEND_PROFILE_PAIRS: frozenset[tuple[str, IspDeviceProfile]] = frozenset(
    {
        ("kathara", "frr"),
        ("kathara", "iosxr"),
        ("kathara", "routeros"),
        ("containerlab", "nokia_srlinux"),
        ("containerlab", "sonic"),
    }
)

DEFAULT_BACKEND_FOR_ISP = "kathara"


def normalize_device_profile(raw: str | IspDeviceProfile | None) -> IspDeviceProfile:
    if raw is None:
        raise ValueError(
            f"device_profile is required; expected one of {SUPPORTED_DEVICE_PROFILES}."
        )
    value = str(raw).strip().lower()
    if value not in SUPPORTED_DEVICE_PROFILES:
        raise ValueError(
            f"Unsupported device_profile {raw!r}; "
            f"expected one of {SUPPORTED_DEVICE_PROFILES}."
        )
    return value  # type: ignore[return-value]


def default_device_profile(backend: str) -> IspDeviceProfile:
    if backend == "kathara":
        return "frr"
    if backend == "containerlab":
        return "nokia_srlinux"
    raise ValueError(f"No default device_profile for backend {backend!r}.")


def validate_backend_profile(backend: str, device_profile: IspDeviceProfile) -> None:
    pair = (backend, device_profile)
    if pair not in SUPPORTED_BACKEND_PROFILE_PAIRS:
        allowed = ", ".join(
            f"{b}+{p}" for b, p in sorted(SUPPORTED_BACKEND_PROFILE_PAIRS)
        )
        raise ValueError(
            f"Unsupported isp pairing backend={backend!r} "
            f"device_profile={device_profile!r}; allowed: {allowed}."
        )
