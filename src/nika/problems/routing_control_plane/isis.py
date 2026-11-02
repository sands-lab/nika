import re

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

_ISIS_ROUTER_RE = re.compile(r"^router isis (\S+)\s*$", re.MULTILINE)
_METRIC_STYLE_RE = re.compile(r"^\s*metric-style (\S+)\s*$", re.MULTILINE)
_IS_TYPE_RE = re.compile(r"^\s*is-type (\S+)\s*$", re.MULTILINE)
_NARROW = "narrow"
_WIDE = "wide"
_LEVEL1 = "level-1"
_LEVEL2 = "level-2-only"


def _isis_block(config: str) -> str:
    """The ``router isis`` block of an FRR config (indented member lines)."""
    match = re.search(r"^router isis \S+\b.*?(?=^\S)", config + "\n!", re.M | re.S)
    return match.group(0) if match else ""


def _isis_process(config: str) -> str | None:
    match = _ISIS_ROUTER_RE.search(config)
    return match[1] if match else None


def _metric_style(config: str) -> str | None:
    match = _METRIC_STYLE_RE.search(_isis_block(config))
    return match[1] if match else None


def _is_type(config: str) -> str | None:
    match = _IS_TYPE_RE.search(_isis_block(config))
    return match[1] if match else None


def _routing_state(problem, host: str) -> dict[str, set]:
    """Read routes and Up adjacencies on the target and its router neighbors."""
    from nika.validation.symptom.routing_probes import (
        _isis_neighbors,
        _isis_routes,
        _native_profile,
        facing_interfaces,
    )

    state = {}
    for router in sorted({host, *facing_interfaces(problem, host)}):
        neighbors = _isis_neighbors(
            problem.runtime, router, profile=_native_profile(problem, router)
        )
        routes = _isis_routes(problem, router)
        if neighbors is None or routes is None:
            raise RuntimeCapabilityError(f"Unreadable IS-IS state on {router!r}.")
        state[f"{router}|routes"] = routes
        state[f"{router}|neighbors"] = {
            (row["system"], row["intf"])
            for row in neighbors
            if row["state"].lower() == "up"
        }
    if not state[f"{host}|neighbors"]:
        raise RuntimeCapabilityError(f"No Up IS-IS adjacency on {host!r}.")
    return state


def _routing_restored(problem, host: str, baseline: dict[str, set]) -> bool:
    state = _routing_state(problem, host)
    return all(values <= state[key] for key, values in baseline.items())


class IsisMetricStyleMismatchParams(BaseModel):
    """Parameters for injecting an IS-IS metric-style mismatch."""

    host_name: str = Field(description="Target router host name.")


class IsisMetricStyleMismatch(ProblemBase):
    """One router originates old-style IS-IS reachability while neighbors use wide.

    Hellos still form the adjacency. Neighbors that originate RFC 5305 extended
    IP reachability (TLV 135) ignore the old IP reachability TLVs (TLV 128),
    and the narrow router ignores TLV 135, so each side's prefixes leave the
    other's routing table. See RFC 5305:
    https://www.rfc-editor.org/rfc/rfc5305
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "isis_metric_style_mismatch"
    description = "IS-IS metric style is inconsistent with neighboring routers."
    effect_protocol = "isis"
    TAGS = ["isis"]
    isp_protocol = {"igp": "isis", "bgp_mode": "none", "rpki": False}
    supported_backends = ("kathara",)
    native_router_operations = frozenset({"isis_metric_style", "set_isis_metric_style"})
    Params = IsisMetricStyleMismatchParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "isis"
        )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: IsisMetricStyleMismatchParams):
        return [node_resource(params.host_name)]

    def _set_style(self, host: str, style: str) -> None:
        ops = native_fault_ops(self, host, "set_isis_metric_style")
        if ops is not None:
            ops.set_isis_metric_style(style)
            return
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        process = _isis_process(config)
        if not process:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: IS-IS is not running on {host!r}."
            )
        self.runtime.exec(
            host, frr_config_command(f"router isis {process}", f"metric-style {style}")
        )
        frr_save_config(self.runtime, host)

    def _style(self, host: str) -> str | None:
        ops = native_fault_ops(self, host, "isis_metric_style")
        if ops is not None:
            return ops.isis_metric_style()
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        return _metric_style(config)

    def inject_fault(self, params: IsisMetricStyleMismatchParams):
        current = self._style(params.host_name)
        if current != _WIDE:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: {params.host_name!r} needs metric-style {_WIDE}, got {current!r}."
            )
        self._healthy_routing = _routing_state(self, params.host_name)
        self._set_style(params.host_name, _NARROW)
        self.logger.info(
            f"Injected IS-IS metric-style {_NARROW} on {params.host_name}."
        )

    def verify_fault(self, params: IsisMetricStyleMismatchParams) -> dict:
        style = self._style(params.host_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=style == _NARROW,
            details={"host": params.host_name, "metric_style": style},
        )

    def recover_fault(self, params: IsisMetricStyleMismatchParams) -> dict:
        baseline = getattr(self, "_healthy_routing", None)
        if not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        self._set_style(params.host_name, _WIDE)
        restored = wait_until(
            lambda: (
                self._style(params.host_name) == _WIDE
                and _routing_restored(self, params.host_name, baseline)
            ),
            120,
            interval_s=3,
        )
        style = self._style(params.host_name)
        return {
            "verified": restored and style == _WIDE,
            "details": {"host": params.host_name, "metric_style": style},
        }


class IsisCircuitLevelMismatchParams(BaseModel):
    """Parameters for injecting an IS-IS level mismatch."""

    host_name: str = Field(description="Target router host name.")


class IsisCircuitLevelMismatch(ProblemBase):
    """One router runs IS-IS at a level its neighbors do not.

    The ISP design is Level-2 only. Setting the instance to Level-1 (FRR and
    IOS XR ``is-type level-1``; RouterOS interface-template ``levels=l1``)
    means hellos no longer share a level, so the adjacency never forms and
    the router's prefixes leave the other routers' tables. An interface
    ``circuit-type level-1`` alone is ignored while the instance stays
    Level-2 only. See RFC 1195:
    https://www.rfc-editor.org/rfc/rfc1195
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "isis_circuit_level_mismatch"
    description = "IS-IS level is inconsistent with neighboring routers."
    effect_protocol = "isis"
    TAGS = ["isis"]
    isp_protocol = {"igp": "isis", "bgp_mode": "none", "rpki": False}
    supported_backends = ("kathara",)
    native_router_operations = frozenset({"isis_level", "set_isis_level"})
    Params = IsisCircuitLevelMismatchParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "isis"
        )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: IsisCircuitLevelMismatchParams):
        return [node_resource(params.host_name)]

    def _set_level(self, host: str, level: str) -> None:
        ops = native_fault_ops(self, host, "set_isis_level")
        if ops is not None:
            ops.set_isis_level(level)
            return
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        process = _isis_process(config)
        if not process:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: IS-IS is not running on {host!r}."
            )
        self.runtime.exec(
            host, frr_config_command(f"router isis {process}", f"is-type {level}")
        )
        frr_save_config(self.runtime, host)

    def _level(self, host: str) -> str | None:
        ops = native_fault_ops(self, host, "isis_level")
        if ops is not None:
            return ops.isis_level()
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        return _is_type(config)

    def inject_fault(self, params: IsisCircuitLevelMismatchParams):
        current = self._level(params.host_name)
        if current not in {_LEVEL2, "level-1-2"}:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: unsupported healthy IS-IS level {current!r} on {params.host_name!r}."
            )
        self._healthy_routing = _routing_state(self, params.host_name)
        self._orig_level = current
        self._set_level(params.host_name, _LEVEL1)
        self.logger.info(f"Injected IS-IS is-type {_LEVEL1} on {params.host_name}.")

    def verify_fault(self, params: IsisCircuitLevelMismatchParams) -> dict:
        level = self._level(params.host_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=level == _LEVEL1,
            details={"host": params.host_name, "isis_level": level},
        )

    def recover_fault(self, params: IsisCircuitLevelMismatchParams) -> dict:
        orig = getattr(self, "_orig_level", None)
        baseline = getattr(self, "_healthy_routing", None)
        if not orig or not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        self._set_level(params.host_name, orig)
        restored = wait_until(
            lambda: (
                self._level(params.host_name) == orig
                and _routing_restored(self, params.host_name, baseline)
            ),
            120,
            interval_s=3,
        )
        level = self._level(params.host_name)
        return {
            "verified": restored and level == orig,
            "details": {"host": params.host_name, "isis_level": level},
        }
