from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nika.problems.base import ProblemBase, build_verify_result
from nika.problems.rca import ProblemGroundTruth


class MultiFaultParams(BaseModel):
    """Resolved injection parameters for a composite multi-fault case."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sub_params: dict[str, BaseModel | None] = Field(default_factory=dict)


def split_prefixed_overrides(
    problem_names: list[str],
    overrides: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Split flat ``problem.field`` overrides or pass through nested mappings."""
    if not overrides:
        return {name: {} for name in problem_names}

    if all(isinstance(value, dict) for value in overrides.values()):
        nested: dict[str, dict[str, str]] = {name: {} for name in problem_names}
        for key, value in overrides.items():
            if key in nested and isinstance(value, dict):
                nested[key] = {str(k): str(v) for k, v in value.items()}
        return nested

    nested = {name: {} for name in problem_names}
    unqualified: dict[str, str] = {}
    for key, value in overrides.items():
        text_key = str(key)
        text_value = str(value)
        if "." in text_key:
            prefix, field = text_key.split(".", 1)
            if prefix in nested:
                nested[prefix][field] = text_value
                continue
        unqualified[text_key] = text_value

    if unqualified:
        for field, value in unqualified.items():
            owners = [name for name in problem_names if field not in nested[name]]
            if len(owners) == 1:
                nested[owners[0]][field] = value
            elif len(owners) > 1:
                raise ValueError(
                    f"Ambiguous --set {field}={value!r} for multi-fault inject; "
                    f"use problem.field syntax (candidates: {owners})."
                )
    return nested


class MultiFaultProblem(ProblemBase):
    """Composite problem that injects multiple sub-faults sequentially."""

    failure_domain = "multiple_faults"
    root_cause_name: list[str] = []
    Params = MultiFaultParams

    def __init__(
        self,
        sub_faults: list[ProblemBase],
        scenario_name: str,
        problem_names: list[str] | None = None,
        **kwargs,
    ):
        super().__init__(scenario_name, **kwargs)
        self.sub_faults = sub_faults
        self.problem_names = list(problem_names or [])
        if not self.problem_names:
            self.problem_names = [
                str(getattr(fault, "root_cause_name", f"fault_{idx}"))
                for idx, fault in enumerate(sub_faults)
            ]
        self._refresh_aggregates()

    @classmethod
    def taxonomy_metadata(cls) -> dict[str, str]:
        return {"failure_domain": "multiple_faults"}

    def _fault_pairs(self) -> list[tuple[str, ProblemBase]]:
        pairs: list[tuple[str, ProblemBase]] = []
        for idx, fault in enumerate(self.sub_faults):
            name = (
                self.problem_names[idx]
                if idx < len(self.problem_names)
                else str(getattr(fault, "root_cause_name", f"fault_{idx}"))
            )
            pairs.append((name, fault))
        return pairs

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

    def resolve_params(
        self,
        params: BaseModel | dict[str, Any] | None = None,
        **overrides: Any,
    ) -> MultiFaultParams | None:
        raw: dict[str, Any] = {}
        if isinstance(params, MultiFaultParams):
            return params
        if isinstance(params, dict):
            raw = dict(params)
        raw.update(overrides)
        nested = split_prefixed_overrides(self.problem_names, raw)
        sub_params: dict[str, BaseModel | None] = {}
        for name, fault in self._fault_pairs():
            piece = nested.get(name, {})
            if hasattr(fault, "parse_params"):
                sub_params[name] = fault.parse_params(piece or None)
            else:
                sub_params[name] = None
        resolved = MultiFaultParams(sub_params=sub_params)
        self._resolved_params = resolved
        return resolved

    def root_cause_resources(self, params=None):
        from nika.problems.rca import FaultResource

        resources: list[FaultResource] = []
        for fault in self.sub_faults:
            piece = fault.root_cause_resources(getattr(fault, "_resolved_params", None))
            resources.extend(piece)
        return resources

    def inject_fault(self, params: MultiFaultParams | None = None) -> None:
        resolved = params if isinstance(params, MultiFaultParams) else self._resolved_params
        if not isinstance(resolved, MultiFaultParams):
            raise ValueError(
                "MultiFaultProblem requires resolved parameters before injection."
            )
        for name, fault in self._fault_pairs():
            parsed = resolved.sub_params.get(name)
            if parsed is not None:
                fault.inject_fault(parsed)
            else:
                fault.inject_fault()
        self._refresh_aggregates()

    def verify_fault(self, params: MultiFaultParams | None = None) -> dict:
        """Verify all sub-faults and aggregate results."""
        resolved = params if isinstance(params, MultiFaultParams) else self._resolved_params
        sub_results = []
        all_verified = True
        for name, fault in self._fault_pairs():
            parsed = (
                resolved.sub_params.get(name)
                if isinstance(resolved, MultiFaultParams)
                else None
            )
            if hasattr(fault, "verify_fault"):
                if parsed is not None:
                    r = fault.verify_fault(parsed)
                else:
                    r = fault.verify_fault()
                sub_results.append(r)
                if not r.get("verified", False):
                    all_verified = False
            else:
                all_verified = False
                sub_results.append(
                    {
                        "verified": False,
                        "fault_type": getattr(fault, "root_cause_name", name),
                        "details": {"error": "no verify_fault method"},
                    }
                )
        self._refresh_aggregates()
        return build_verify_result(
            fault_type=str(self.root_cause_name),
            verified=all_verified,
            details={"sub_results": sub_results},
        )

    def get_ground_truth(self) -> ProblemGroundTruth:
        from nika.problems.rca.materialize import build_multi_ground_truth

        self._refresh_aggregates()
        return build_multi_ground_truth(
            self.sub_faults, failure_domain="multiple_faults"
        )

    def get_task_description(self) -> str:
        base = super().get_task_description()
        symptoms: list[str] = []
        seen: set[str] = set()
        for fault in self.sub_faults:
            text = (getattr(fault, "symptom_desc", "") or "").strip()
            if text and text not in seen:
                seen.add(text)
                symptoms.append(text)
        if not symptoms:
            return base
        symptom_block = "\n".join(f"- {item}" for item in symptoms)
        return (
            f"{base}\n\nReported symptoms include:\n{symptom_block}\n\n"
            "Multiple independent faults may be present; identify each root cause."
        )
