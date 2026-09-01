from __future__ import annotations

import textwrap
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ValidationError

from nika.problems.context import init_problem
from nika.problems.rca import (
    FaultResource,
    ProblemGroundTruth,
    RootCause,
    UnresolvedRootCauseError,
)
from nika.runtime.base import RuntimeCapabilityError

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime


class FailureDomain(StrEnum):
    def __new__(cls, value, description):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    LINK_INTERFACE = (
        "link_interface",
        "Physical links and network interfaces",
    )
    ROUTING_CONTROL_PLANE = (
        "routing_control_plane",
        "BGP, OSPF, and MPLS control-plane computation, signaling, and adjacency",
    )
    FORWARDING_ENCAPSULATION_POLICY = (
        "forwarding_encapsulation_policy",
        "Packet forwarding, encapsulation, and data-plane policy",
    )
    SERVICE_NETWORKING = (
        "service_networking",
        "Service virtual IPs, service forwarding, and load balancing",
    )
    MANAGEMENT_ORCHESTRATION_PLANE = (
        "management_orchestration_plane",
        "Network management systems, controllers, and orchestration APIs",
    )
    ADDRESSING_NEIGHBOR_NAMING = (
        "addressing_neighbor_naming",
        "Address assignment, neighbor state, and naming services",
    )
    ENDPOINT_APPLICATION = (
        "endpoint_application",
        "Endpoint availability, resource state, and application behavior",
    )
    TRAFFIC_QUEUEING_RESOURCE = (
        "traffic_queueing_resource",
        "Traffic load, queueing, link capacity, and shared network resources",
    )
    SECURITY = (
        "security",
        "Attacks, spoofing, and poisoning",
    )


class ProblemMeta(BaseModel):
    failure_domain: FailureDomain
    root_cause_name: str
    description: str


class ProblemBase:
    """Core base class for fault definition, injection, verification, and truth."""

    failure_domain: ClassVar[FailureDomain | str | None] = None
    root_cause_name: ClassVar[str] = ""
    # Short meaning of the failure ID for agent ontology / registry.
    # Must not leak injection method, artifacts, or differential probe shortcuts.
    description: ClassVar[str] = ""
    symptom_desc: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel] | None] = None
    META: ClassVar[ProblemMeta | None] = None
    TAGS: ClassVar[list[str]] = []
    # Optional coverage-column ids when TAGS alone would match too broadly.
    # ``None`` means TAGS subset matching only. Values are column ids as in
    # ``coverage_columns`` (scenario name, or ``isp_<topo>/<config>``).
    COMPATIBLE_COLUMNS: ClassVar[frozenset[str] | None] = None
    required_capabilities: ClassVar[tuple[str, ...] | list[str]] = ()
    supported_backends: ClassVar[tuple[str, ...] | list[str] | None] = None
    # Optional protocol whose adjacency effect this failure declares.
    effect_protocol: ClassVar[str | None] = None
    effect_property: ClassVar[str | None] = None

    @classmethod
    def matches_column(cls, column: str, column_tags: frozenset[str]) -> bool:
        """Return whether this failure can inject usefully on ``column``."""
        allowed = cls.COMPATIBLE_COLUMNS
        if allowed is not None and column not in allowed:
            return False
        return frozenset(cls.TAGS).issubset(column_tags)

    @classmethod
    def compatible_scenarios(cls) -> frozenset[str] | None:
        """Scenario names implied by ``COMPATIBLE_COLUMNS``, or ``None`` if open."""
        if cls.COMPATIBLE_COLUMNS is None:
            return None
        return frozenset(column.partition("/")[0] for column in cls.COMPATIBLE_COLUMNS)

    net_env: NetworkEnvBase
    runtime: LabRuntime

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "META" not in cls.__dict__:
            domain = cls.__dict__.get("failure_domain")
            name = cls.__dict__.get("root_cause_name")
            if domain is not None and isinstance(name, str) and name:
                description = (
                    cls.__dict__.get("description")
                    or cls.__dict__.get("symptom_desc")
                    or name
                )
                cls.META = ProblemMeta(
                    failure_domain=domain,
                    root_cause_name=name,
                    description=description,
                )
        for method_name in ("inject_fault", "verify_fault"):
            method = cls.__dict__.get(method_name)
            if method is None or getattr(method, "_nika_capability_checked", False):
                continue

            @wraps(method)
            def checked(
                self: "ProblemBase",
                *args: Any,
                __method=method,
                __name=method_name,
                **kwargs: Any,
            ) -> Any:
                self.check_runtime_compatible(operation=__name)
                return __method(self, *args, **kwargs)

            checked._nika_capability_checked = True  # type: ignore[attr-defined]
            setattr(cls, method_name, checked)

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        try:
            super().__init__()  # type: ignore[misc]
        except TypeError:
            pass
        self.results = getattr(self, "results", {})
        self.scenario_name = scenario_name
        self._resolved_params: BaseModel | dict[str, Any] | None = None
        if scenario_name is not None or kwargs:
            self.init_runtime(scenario_name, **kwargs)

    @classmethod
    def taxonomy_metadata(cls) -> dict[str, str]:
        """Return the benchmark taxonomy dimensions for this failure."""
        if cls.META is None:
            return {}
        return {
            key: str(value)
            for key, value in cls.META.model_dump(include={"failure_domain"}).items()
        }

    def init_runtime(self, scenario_name: str | None, **kwargs: Any) -> None:
        """Resolve and attach the network environment and runtime once."""
        self.net_env, self.runtime = init_problem(scenario_name, **kwargs)
        self.check_runtime_compatible(operation="init")

    def parse_params(
        self, params: BaseModel | dict[str, Any] | None = None, **overrides: Any
    ) -> BaseModel | None:
        """Parse raw parameter input through the problem's ``Params`` model."""
        params_class = self.Params
        if params is None:
            data: dict[str, Any] = {}
        elif isinstance(params, BaseModel):
            if params_class is not None and isinstance(params, params_class):
                return params
            data = params.model_dump(exclude_none=True)
        elif isinstance(params, dict):
            data = dict(params)
        else:
            raise TypeError(
                f"Unsupported parameter payload for {type(self).__name__}: {type(params).__name__}"
            )

        data.update(overrides)
        if params_class is None:
            if data:
                raise ValueError(
                    f"Problem '{self.root_cause_name}' does not accept parameters."
                )
            return None
        try:
            parsed = params_class.model_validate(data)
        except ValidationError as exc:
            raise ValueError(
                f"Invalid or missing parameters for '{self.root_cause_name}': {exc}. "
                f"Run `nika failure describe {self.root_cause_name}` for required fields."
            ) from exc
        self._resolved_params = parsed
        return parsed

    def resolve_params(
        self, params: BaseModel | dict[str, Any] | None = None, **overrides: Any
    ) -> BaseModel | None:
        """Resolve injection parameters; subclasses may fill derived defaults."""
        return self.parse_params(params, **overrides)

    @property
    def lab_backend(self) -> str:
        return self.runtime.backend

    def root_cause_resources(self, params: Any = None) -> list[FaultResource]:
        """Return the mutated catalog resources for this injected fault.

        Every concrete failure must override this. Default raises so a missing
        mapping cannot silently fall back.
        """
        raise UnresolvedRootCauseError(
            f"{type(self).__name__} must implement root_cause_resources()."
        )

    def get_ground_truth(self) -> ProblemGroundTruth:
        """Return detection + structured RCA ground truth."""
        params = self._resolved_params
        resources = self.root_cause_resources(params)
        if not resources:
            raise UnresolvedRootCauseError(
                f"{type(self).__name__}.root_cause_resources() returned no objects."
            )
        name = self.root_cause_name
        if isinstance(name, str):
            fault_type = name
        else:
            names = list(name or [])
            if len(names) != 1:
                raise UnresolvedRootCauseError(
                    f"Expected a single fault_type on {type(self).__name__}, got {names!r}."
                )
            fault_type = names[0]
        root_causes = [
            RootCause(resource=resource, fault_type=fault_type)
            for resource in resources
        ]
        return ProblemGroundTruth(
            is_anomaly=True,
            root_causes=root_causes,
            failure_domain=str(self.failure_domain or ""),
        )

    def get_task_description(self) -> str:
        """Return the agent-facing diagnostic task prompt."""
        diagnostic_prompt = """\
            You are provided with the following network description and its current state:
            {net_desc}

            Your goal is to analyze the network condition and, if needed, use the available tools.
            You need to generate a troubeshooting diagnosis report.
            The report should reflect your assessment of the network's health, indicate any abnormal behavior you identify, and describe relevant findings based on your analysis.

            Focus on producing an informative and coherent diagnostic report derived from the network state.
            Do not need to propose any solutions or remediation steps at this stage.
            """
        tmpl = textwrap.dedent(diagnostic_prompt)
        return tmpl.format(net_desc=self.net_env.get_info()).strip()

    def check_runtime_compatible(
        self, *, operation: Literal["init", "inject_fault", "verify_fault"] | str
    ) -> None:
        net_env = getattr(self, "net_env", None)
        runtime = getattr(self, "runtime", None)
        backend = getattr(net_env, "backend", None) or getattr(runtime, "backend", None)
        supported = self.supported_backends
        if supported and backend and backend not in supported:
            allowed = ", ".join(str(item) for item in supported)
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot {operation}: backend {backend!r} is not supported. "
                f"Supported backends: {allowed}."
            )

        required = tuple(str(name) for name in self.required_capabilities)
        if required and hasattr(runtime, "require_capabilities"):
            try:
                runtime.require_capabilities(*required)
            except RuntimeCapabilityError as exc:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot {operation}: {exc}"
                ) from exc
        else:
            missing = [name for name in required if not hasattr(runtime, name)]
            if missing:
                missing_text = ", ".join(missing)
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot {operation}: runtime for backend {backend!r} "
                    f"lacks required capabilities: {missing_text}."
                )


def build_verify_result(
    fault_type: str,
    verified: bool,
    details: dict,
) -> dict:
    return {
        "verified": verified,
        "fault_type": fault_type,
        "details": details,
    }
