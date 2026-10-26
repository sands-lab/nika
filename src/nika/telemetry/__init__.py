"""Optional scenario telemetry enabled per case with ``telemetry: [<kind>, ...]``."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from nika.runtime.meta import meta_get

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

NETFLOW = "netflow"
PROMETHEUS = "prometheus"
SYSLOG = "syslog"
TELEMETRY_KINDS: tuple[str, ...] = (NETFLOW, PROMETHEUS, SYSLOG)
# Out-of-band management LAN on Kathara, and the extra sFlow LAN on Containerlab.
TELEMETRY_SUBNET = "172.28.0.0/16"
# Prefix of the Kathara links of the out-of-band management network shared by
# every telemetry kind. Link names stay on the host; not a scenario link.
TELEMETRY_LINK_PREFIX = "nika_telemetry_"
# Names devices see: the VRF that holds each configured device's management
# interface, and the directory of the telemetry configuration and start script.
# Agents inspect devices, so these do not name the benchmark.
MGMT_VRF = "mgmt"
CONFIG_DIR = "/etc/telemetry"
# Seconds to wait after the lab is verified for collectors and exporters to run:
# a base plus a share per device, since a large lab loads the host while it starts.
TELEMETRY_READY_BASE_SEC = 60
TELEMETRY_READY_PER_DEVICE_SEC = 1
# Concurrent per-device execs when probing, configuring, and checking devices;
# large labs have hundreds of devices.
DEVICE_EXEC_WORKERS = 16


def telemetry_ready_timeout(plan: dict[str, Any]) -> float:
    """Seconds to wait for a configured plan's collectors and exporters to run."""
    return TELEMETRY_READY_BASE_SEC + TELEMETRY_READY_PER_DEVICE_SEC * len(
        plan["devices"]
    )


def telemetry_link(name: str) -> str:
    """Kathara link between ``name`` and the management switch."""
    return f"{TELEMETRY_LINK_PREFIX}{name}"


def is_telemetry_link(link: str) -> bool:
    """Whether a Kathara link belongs to the management network, not the scenario."""
    return link.startswith(TELEMETRY_LINK_PREFIX)


def normalize_telemetry(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Return the requested telemetry kinds, sorted and validated."""
    if value is None:
        return ()
    kinds = {value} if isinstance(value, str) else set(value)
    unknown = sorted(kinds - set(TELEMETRY_KINDS))
    if unknown:
        raise ValueError(
            f"Unknown telemetry {unknown}; supported: {list(TELEMETRY_KINDS)}."
        )
    return tuple(sorted(kinds))


def telemetry_field(value: str | Iterable[str] | None) -> dict[str, list[str]]:
    """Return ``{"telemetry": [...]}`` for a case row, or ``{}`` when unset.

    Rows without telemetry omit the field so their identity stays unchanged.
    """
    kinds = normalize_telemetry(value)
    return {"telemetry": list(kinds)} if kinds else {}


def require_telemetry_backend(kinds: tuple[str, ...], backend: str) -> None:
    """Reject telemetry on backends that cannot attach it."""
    if kinds and backend not in {"kathara", "containerlab"}:
        raise ValueError(
            f"Telemetry {list(kinds)} is only supported on Kathara or Containerlab, "
            f"not {backend!r}."
        )


def telemetry_for_session(meta: dict[str, Any] | Any) -> tuple[str, ...]:
    """Return the telemetry kinds a session was started with."""
    return normalize_telemetry(meta_get(meta, "telemetry", scenario_params=True))


def apply_telemetry(net_env: NetworkEnvBase, kinds: Iterable[str]) -> dict[str, Any]:
    """Wire the management network into the built, not yet deployed, lab.

    Returns the telemetry plan to persist with the session.
    """
    from nika.telemetry import management

    kinds = normalize_telemetry(kinds)
    if getattr(net_env, "backend", None) == "containerlab":
        from nika.telemetry import containerlab as clab_telemetry

        return clab_telemetry.attach(net_env, kinds)
    return management.attach(net_env, kinds)


def configure_telemetry(runtime: LabRuntime, plan: dict[str, Any]) -> dict[str, Any]:
    """Configure each requested kind on the devices that support it.

    Runs in the lab check phase, once the containers are up. A device without
    support is left unconfigured; that is not an error.
    """
    from nika.telemetry import management

    if plan.get("backend") == "containerlab":
        from nika.telemetry import containerlab as clab_telemetry

        return clab_telemetry.configure(runtime, plan)
    return management.configure(runtime, plan)


def telemetry_running(runtime: LabRuntime, plan: dict[str, Any]) -> bool:
    """Every requested kind has its collector service and device exporters running."""
    from nika.telemetry import management

    if plan.get("backend") == "containerlab":
        from nika.telemetry import containerlab as clab_telemetry

        return clab_telemetry.running(runtime, plan)
    return management.running(runtime, plan)
