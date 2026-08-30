from __future__ import annotations

import asyncio

from nika.problems.problem_base import (
    ProblemBase,
    ProblemGroundTruth,
    RootCauseCategory,
    build_verify_result,
)


class MultiFaultProblem(ProblemBase):
    """Composite problem that injects multiple sub-faults in parallel."""

    root_cause_category = RootCauseCategory.MULTIPLE_FAULTS
    root_cause_name: list[str] = []

    def __init__(self, sub_faults: list[ProblemBase], scenario_name: str, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.sub_faults = sub_faults
        self._refresh_aggregates()

    def _refresh_aggregates(self) -> None:
        root_names: list[str] = []
        for fault in self.sub_faults:
            name = fault.root_cause_name
            if isinstance(name, str):
                if name:
                    root_names.append(name)
            else:
                root_names.extend(name)
        self.root_cause_name = root_names

    async def _inject_fault_async(self) -> None:
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(None, fault.inject_fault) for fault in self.sub_faults
        ]
        await asyncio.gather(*tasks)

    def root_cause_resources(self, params=None):
        from nika.problems.root_cause import FaultResource

        resources: list[FaultResource] = []
        for fault in self.sub_faults:
            piece = fault.root_cause_resources(getattr(fault, "_resolved_params", None))
            resources.extend(piece)
        return resources

    def inject_fault(self, params=None) -> None:
        del params
        asyncio.run(self._inject_fault_async())
        self._refresh_aggregates()

    def verify_fault(self) -> dict:
        """Verify all sub-faults and aggregate results."""
        sub_results = []
        all_verified = True
        for fault in self.sub_faults:
            if hasattr(fault, "verify_fault"):
                r = fault.verify_fault()
                sub_results.append(r)
                if not r.get("verified", False):
                    all_verified = False
            else:
                all_verified = False
                sub_results.append(
                    {
                        "verified": False,
                        "root_cause_name": getattr(fault, "root_cause_name", "unknown"),
                        "details": {"error": "no verify_fault method"},
                    }
                )
        self._refresh_aggregates()
        return build_verify_result(
            root_cause_name=str(self.root_cause_name),
            faulty_devices=self.faulty_devices,
            verified=all_verified,
            details={"sub_results": sub_results},
        )

    def get_ground_truth(self) -> ProblemGroundTruth:
        from nika.problems.ground_truth import build_multi_ground_truth

        self._refresh_aggregates()
        return build_multi_ground_truth(
            self.sub_faults, category=str(self.root_cause_category)
        )
