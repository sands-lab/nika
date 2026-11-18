from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nika.net_env.contract import ValidationContract, ValidationReport
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.service import ensure_batfish_service
from nika.validation.batfish.snapshot import build_isp_snapshot
from nika.validation.batfish.verifier import BatfishVerifier
from nika.validation.dispatcher import VerifierDispatcher

STATIC_VALIDATION_FILENAME = "{verifier}-validation.json"
_BATFISH_HOST = "127.0.0.1"
_BATFISH_PORT = 9996
_STATIC_VERIFIERS = ("batfish",)


def run_static_validation(
    *,
    net_env: Any,
    contract: ValidationContract,
    artifact_dir: str | Path,
) -> dict[str, ValidationReport]:
    """Run NIKA's built-in static validators when the environment is supported."""
    build_snapshot = _batfish_snapshot_builder(net_env, contract)
    if build_snapshot is None:
        return {}

    artifact_root = Path(artifact_dir)
    snapshot = build_snapshot(artifact_root)
    service_error: Exception | None = None
    try:
        ensure_batfish_service(host=_BATFISH_HOST, port=_BATFISH_PORT)
    except Exception as exc:  # noqa: BLE001 - verifier records the failure
        service_error = exc
    verifier = (
        BatfishVerifier(client=_FailedBatfishClient(service_error))
        if service_error is not None
        else BatfishVerifier(host=_BATFISH_HOST, port=_BATFISH_PORT)
    )
    reports = VerifierDispatcher([verifier]).run(
        contract, {"batfish": snapshot}, names=_STATIC_VERIFIERS
    )
    for name, report in reports.items():
        report.write(artifact_root / STATIC_VALIDATION_FILENAME.format(verifier=name))
    return reports


def _batfish_snapshot_builder(
    net_env: Any, contract: ValidationContract
) -> Callable[[Path], ValidationSnapshot] | None:
    """Return the Batfish snapshot adapter for this run, or ``None`` if unsupported."""
    from nika.workflows.benchmark.isp_options import is_isp_scenario

    if getattr(net_env, "device_profile", None) == "sonic" and hasattr(
        net_env, "sonic_routers"
    ):
        from nika.validation.batfish.sonic import build_sonic_snapshot

        return lambda root: build_sonic_snapshot(net_env, contract, root)
    if (
        is_isp_scenario(contract.scenario)
        and getattr(net_env, "backend", None) == "kathara"
        and getattr(net_env, "device_profile", None) == "frr"
        and all(
            hasattr(net_env, attribute)
            for attribute in ("plan", "traffic", "deployment_configs")
        )
    ):
        return lambda root: build_isp_snapshot(
            root=root,
            contract=contract,
            plan=net_env.plan,
            traffic=net_env.traffic,
            deployment_configs=net_env.deployment_configs,
        )
    return None


class _FailedBatfishClient:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def initialize(self, snapshot):
        raise self._error

    def execute(self, question):  # pragma: no cover - initialize always raises
        raise self._error

    def sanity_checks(self):  # pragma: no cover - initialize always raises
        raise self._error
