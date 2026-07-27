from __future__ import annotations

import textwrap
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from nika.problems.context import init_problem
from nika.runtime.base import RuntimeCapabilityError

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime


class RootCauseCategory(StrEnum):
    def __new__(cls, value, description):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    LINK_FAILURE = (
        "link_failure",
        "Link failures: physical disconnections, interface down",
    )
    END_HOST_FAILURE = (
        "end_host_failure",
        "Host misconfiguration: IP, gateway, DNS, DHCP issues",
    )
    NETWORK_NODE_ERROR = (
        "network_node_error",
        "Router/switch crashes, reboots, high CPU/memory usage",
    )
    RESOURCE_CONTENTION = (
        "resource_contention",
        "Resource contention: bandwidth saturation, buffer overflows",
    )
    MISCONFIGURATION = (
        "misconfiguration",
        "Configuration errors: wrong IP, ACL, routing protocol settings",
    )
    NETWORK_UNDER_ATTACK = (
        "network_under_attack",
        "Security attacks: DDoS, BGP hijack, MITM, spoofing",
    )
    MULTIPLE_FAULTS = ("multiple_faults", "Multiple simultaneous faults in the network")


class ProblemMeta(BaseModel):
    root_cause_category: RootCauseCategory
    root_cause_name: str
    description: str


class ProblemGroundTruth(BaseModel):
    is_anomaly: bool = Field(
        description="Whether an anomaly is present in the network."
    )
    faulty_devices: list[str] = Field(description="Faulty device or component names.")
    root_cause_category: str = Field(description="Root cause category identifier.")
    root_cause_name: list[str] = Field(description="Root cause name(s).")
    detailed_cause: str = Field(
        default="", description="Detailed description of the root cause."
    )


class ProblemBase:
    """Core base class for fault definition, injection, verification, and truth."""

    root_cause_category: ClassVar[RootCauseCategory | str | None] = None
    root_cause_name: ClassVar[str] = ""
    symptom_desc: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel] | None] = None
    META: ClassVar[ProblemMeta | None] = None
    TAGS: ClassVar[list[str]] = []
    required_capabilities: ClassVar[tuple[str, ...] | list[str]] = ()
    supported_backends: ClassVar[tuple[str, ...] | list[str] | None] = None

    net_env: NetworkEnvBase
    runtime: LabRuntime

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "META" not in cls.__dict__:
            category = cls.__dict__.get("root_cause_category")
            name = cls.__dict__.get("root_cause_name")
            if category is not None and isinstance(name, str) and name:
                description = cls.__dict__.get("symptom_desc") or name
                cls.META = ProblemMeta(
                    root_cause_category=category,
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
        self.faulty_devices: list[str] = []
        if scenario_name is not None or kwargs:
            self.init_runtime(scenario_name, **kwargs)

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
            return params_class.model_validate(data)
        except ValidationError as exc:
            raise ValueError(
                f"Invalid or missing parameters for '{self.root_cause_name}': {exc}. "
                f"Run `nika failure describe {self.root_cause_name}` for required fields."
            ) from exc

    def resolve_params(
        self, params: BaseModel | dict[str, Any] | None = None, **overrides: Any
    ) -> BaseModel | None:
        """Resolve injection parameters; subclasses may fill derived defaults."""
        return self.parse_params(params, **overrides)

    def set_faulty_devices(self, devices: list[str | None]) -> None:
        """Replace the faulty-device set while preserving order."""
        self.faulty_devices = []
        for device in devices:
            if device and device not in self.faulty_devices:
                self.faulty_devices.append(device)

    @property
    def lab_backend(self) -> str:
        return self.runtime.backend

    def get_ground_truth(self) -> ProblemGroundTruth:
        """Return unified detection, localization, and RCA ground truth."""
        root_cause_name = self.root_cause_name
        if isinstance(root_cause_name, str):
            root_names = [root_cause_name] if root_cause_name else []
        else:
            root_names = list(root_cause_name)
        assert self.faulty_devices, (
            "Faulty devices not set before building ground truth."
        )
        return ProblemGroundTruth(
            is_anomaly=True,
            faulty_devices=list(self.faulty_devices),
            root_cause_category=str(self.root_cause_category),
            root_cause_name=root_names,
            detailed_cause=getattr(self, "symptom_desc", "") or "",
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
    root_cause_name: str,
    faulty_devices: list[str],
    verified: bool,
    details: dict,
) -> dict:
    return {
        "verified": verified,
        "root_cause_name": root_cause_name,
        "faulty_devices": list(faulty_devices),
        "details": details,
    }
