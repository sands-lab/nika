"""Authored scenario requirements, routing completion choices and operating conditions."""

from typing import Literal

from pydantic import Field, model_validator

from nika.net_env.contract import ContractModel, ValidationContract
from nika.net_env.synthesis.changes import DesignChange
from nika.net_env.synthesis.completion import RoutingCompletion
from nika.net_env.synthesis.requirements import RoutingRequirement


class ScenarioIncident(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    host_name: str
    intf_name: str
    requirements: ValidationContract
    runtime_requirements: ValidationContract | None = None


class RoutingMutation(ContractModel):
    """A static counterexample: a wrong configuration and the intents it breaks.

    ``changes`` state the error on the routing design; netlab renders them
    when static analysis reads netlab output. ``old`` and ``new`` replace one
    statement of the configuration that NIKA renders itself.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    node: str
    old: str | None = None
    new: str | None = None
    changes: tuple[DesignChange, ...] = ()
    expected_failed_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _edit(self):
        if (self.old is None) != (self.new is None):
            raise ValueError("A text edit needs both old and new")
        if self.old is None and not self.changes:
            raise ValueError("A counterexample needs design changes or a text edit")
        return self


class RoutingRuntimeMutation(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    node: str
    commands: tuple[str, ...] = Field(min_length=1)
    recovery: tuple[str, ...] = Field(min_length=1)
    expected_failed_ids: tuple[str, ...] = Field(min_length=1)


class RoutingFailureCheck(ContractModel):
    """A registered configuration failure exercised against original requirements."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    problem: str
    params: dict[str, str]
    restore_nodes: tuple[str, ...] = Field(min_length=1)
    requirements: ValidationContract | None = None
    recovery_commands: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    restore_nft_nodes: tuple[str, ...] = ()
    expected_failed_ids: tuple[str, ...] = Field(min_length=1)
    # Original intents the fault may or may not violate, depending on choices
    # that completion does not pin down. Each still requires a concrete result.
    unconstrained_ids: tuple[str, ...] = ()
    # The fault's static model: design changes that netlab renders, and text
    # edits of the configuration that NIKA renders itself.
    changes: tuple[DesignChange, ...] = ()
    static_edits: dict[str, tuple[str, str]] = Field(default_factory=dict)


class ScenarioDeployment(ContractModel):
    """Existing deployment choices retained by a scenario experiment."""

    backend: Literal["kathara", "containerlab"] | None = None
    igp: Literal["ospf", "isis"] | None = None
    bgp_mode: Literal["none", "ibgp_rr", "ebgp"] | None = None
    device_profile: str | None = None
    rpki: bool | None = None


# Routing models whose scenario constructor takes ``routing_model`` directly.
ENV_ROUTING_MODELS = frozenset(
    {"srl-bgp", "iosxr-bgp", "routeros-bgp", "iosxr-isp", "routeros-isp"}
)


class ScenarioExperiment(ContractModel):
    scenario: str
    routing_model: Literal[
        "frr",
        "linux-static",
        "srl-bgp",
        "iosxr-bgp",
        "routeros-bgp",
        "frr-isis",
        "srl-isp",
        "iosxr-isp",
        "routeros-isp",
        "netkat",
    ] = "frr"
    topo_size: Literal["s", "m", "l"] | None = None
    deployment: ScenarioDeployment = Field(default_factory=ScenarioDeployment)
    healthy: ValidationContract
    completion: RoutingCompletion | tuple[RoutingCompletion, ...] | None = None
    behavior: tuple[RoutingRequirement, ...] = ()
    runtime_requirements: ValidationContract | None = None
    counterexamples: tuple[RoutingMutation, ...] = ()
    runtime_counterexamples: tuple[RoutingRuntimeMutation, ...] = ()
    failure_checks: tuple[RoutingFailureCheck, ...] = ()
    solver_timeout_sec: float = Field(default=300, ge=1, le=3600)
    convergence_timeout_sec: float = Field(default=300, ge=1, le=3600)
    incidents: tuple[ScenarioIncident, ...]
