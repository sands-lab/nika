"""Optional scenario telemetry enabled per case with ``telemetry: [<kind>, ...]``."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from nika.runtime.meta import meta_get

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

NETFLOW = "netflow"
TELEMETRY_KINDS: tuple[str, ...] = (NETFLOW,)
# Out-of-band telemetry network: the shared export LAN and, on each exporting
# device, the VRF that holds its export interface. Not a scenario link.
TELEMETRY_NETWORK = "nika_telemetry"
# Seconds to wait after the lab is verified for exporters and collectors to run.
TELEMETRY_READY_TIMEOUT_SEC = 60


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
    """Reject telemetry on backends that cannot attach it (only Kathara can)."""
    if kinds and backend != "kathara":
        raise ValueError(
            f"Telemetry {list(kinds)} is only supported on the Kathara backend, "
            f"not {backend!r}."
        )


def telemetry_for_session(meta: dict[str, Any] | Any) -> tuple[str, ...]:
    """Return the telemetry kinds a session was started with."""
    return normalize_telemetry(meta_get(meta, "telemetry", scenario_params=True))


def apply_telemetry(net_env: NetworkEnvBase, kinds: Iterable[str]) -> dict[str, Any]:
    """Wire each telemetry kind's network into the built, not yet deployed, lab.

    Returns per-kind metadata to persist with the session.
    """
    from nika.run_config.loader import get_run_config
    from nika.telemetry import netflow

    settings = get_run_config().nika.telemetry
    applied: dict[str, Any] = {}
    if NETFLOW in normalize_telemetry(kinds):
        applied[NETFLOW] = netflow.attach(net_env, settings.netflow.subnet)
    return applied


def configure_telemetry(runtime: LabRuntime, applied: dict[str, Any]) -> dict[str, Any]:
    """Configure each wired telemetry kind on the devices that support it.

    Runs in the lab check phase, once the containers are up. A device without
    support is left unconfigured; that is not an error.
    """
    from nika.telemetry import netflow

    if NETFLOW not in applied:
        return applied
    return {**applied, NETFLOW: netflow.configure(runtime, applied[NETFLOW])}


def telemetry_running(runtime: LabRuntime, applied: dict[str, Any]) -> bool:
    """Every attached telemetry kind has its collectors and exporters running."""
    from nika.telemetry import netflow

    return NETFLOW not in applied or netflow.export_running(runtime, applied[NETFLOW])
