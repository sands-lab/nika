from __future__ import annotations

from collections.abc import Iterable

from nika.net_env.contract import ValidationContract, ValidationReport
from nika.validation.base import ValidationSnapshot, ValidationVerifier


class VerifierDispatcher:
    """Run named semantic oracles against one immutable contract and snapshot."""

    def __init__(self, verifiers: Iterable[ValidationVerifier]) -> None:
        self._verifiers = {verifier.name: verifier for verifier in verifiers}

    def run(
        self,
        contract: ValidationContract,
        snapshots: dict[str, ValidationSnapshot],
        *,
        names: Iterable[str],
    ) -> dict[str, ValidationReport]:
        reports: dict[str, ValidationReport] = {}
        for name in names:
            verifier = self._verifiers.get(name)
            if verifier is None:
                raise ValueError(f"unknown validation verifier {name!r}")
            snapshot = snapshots.get(name)
            if snapshot is None:
                raise ValueError(f"missing snapshot for verifier {name!r}")
            reports[name] = verifier.verify(contract, snapshot)
        return reports
