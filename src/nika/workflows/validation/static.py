from __future__ import annotations

from pathlib import Path
from typing import Any

from nika.net_env.contract import ValidationContract, ValidationReport
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
    if not _supports_batfish(net_env, contract):
        return {}

    artifact_root = Path(artifact_dir)
    snapshot = build_isp_snapshot(
        root=artifact_root,
        contract=contract,
        plan=net_env.plan,
        traffic=net_env.traffic,
        deployment_configs=net_env.deployment_configs,
    )
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


def _supports_batfish(net_env: Any, contract: ValidationContract) -> bool:
    """Return whether the built-in Batfish snapshot adapter supports this run."""
    from nika.workflows.benchmark.isp_options import is_isp_scenario

    return (
        is_isp_scenario(contract.scenario)
        and getattr(net_env, "backend", None) == "kathara"
        and getattr(net_env, "device_profile", None) == "frr"
        and all(
            hasattr(net_env, attribute)
            for attribute in ("plan", "traffic", "deployment_configs")
        )
    )


class _FailedBatfishClient:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def initialize(self, snapshot):
        raise self._error

    def execute(self, question):  # pragma: no cover - initialize always raises
        raise self._error

    def sanity_checks(self):  # pragma: no cover - initialize always raises
        raise self._error
