import re

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.net_env.isp.identity import list_isp_base_scenarios
from nika.problems.rca import node_resource
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.polling import wait_until
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    contains,
    router_cli,
    vtysh,
)
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.runtime.base import RuntimeCapabilityError
from nika.service.containerlab.srl_api import srl_instance, srl_linux_intf
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
    block = _isis_block(config)
    if not block:
        return None
    # FRR omits its default wide metric style from running-config.
    match = _METRIC_STYLE_RE.search(block)
    return match[1] if match else _WIDE


def _is_type(config: str) -> str | None:
    match = _IS_TYPE_RE.search(_isis_block(config))
    return match[1] if match else None


def _hello_authentication(config: str) -> dict[str, str]:
    """Read authentication on FRR circuits that send IS-IS hellos."""
    circuits = {}
    for match in re.finditer(
        r"^interface (\S+)[^\n]*\n(.*?)(?=^\S|\Z)", config, re.M | re.S
    ):
        interface, block = match.groups()
        if not re.search(r"^ ip router isis \S+", block, re.M):
            continue
        if re.search(r"^ isis passive\s*$", block, re.M):
            continue
        auth = re.search(r"^ isis password (\S+)", block, re.M)
        circuits[interface] = auth[1] if auth else "none"
    return circuits


def _srl_authentication(instance: dict) -> dict[str, str]:
    circuits = {}
    for interface in instance.get("interface", []):
        if interface.get("passive", False):
            continue
        auth = {
            **instance.get("authentication", {}),
            **interface.get("authentication", {}),
        }
        mode = auth.get("key", {}).get("crypto-algorithm") or (
            "keychain" if auth.get("keychain") else None
        )
        hello = auth.get("hello-authentication", {})
        circuits[srl_linux_intf(interface["interface-name"])] = (
            "md5"
            if mode == "hmac-md5"
            and hello.get("generate")
            and hello.get("check-received") == "strict"
            else (mode or "none")
        )
    return circuits


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


def _isis_oracle(problem, host, setting):
    """Read the target and its peers to distinguish IS-IS configuration faults."""
    from nika.net_env.utils.routeros.parse import routeros_terse_rows
    from nika.validation.symptom.routing_probes import facing_interfaces

    peers = sorted(facing_interfaces(problem, host))
    if not peers:
        raise RuntimeCapabilityError(f"No IS-IS router linked to {host}")
    steps = []
    descriptions = {
        "metric": "the target uses narrow metrics while its peers use wide metrics, distinguishing incompatible reachability TLVs from level or authentication mismatch",
        "level": "the target runs Level 1 while its peers run Level 2 only, distinguishing disjoint levels from hello authentication or metric mismatch",
        "auth": "the target requires MD5 hello authentication while its peers send unauthenticated hellos, distinguishing authentication rejection from level or metric mismatch",
    }
    observed = {}

    def value(output, profile):
        if (
            getattr(problem.net_env, "backend", None) == "containerlab"
        ) and profile is None:
            instance = srl_instance(output, "isis")
            if setting == "metric":
                return next(
                    row.get("metric-style", "wide")
                    for row in instance["level"]
                    if row["level-number"] == 2
                )
            if setting == "level":
                return {"L1": _LEVEL1, "L2": _LEVEL2, "L1L2": "level-1-2"}.get(
                    instance["level-capability"]
                )
            return _srl_authentication(instance)
        if profile == "routeros":
            rows = routeros_terse_rows(output)
            key = "metric-type" if setting == "metric" else "levels"
            values = {row[key] for row in rows if key in row}
            if len(values) != 1:
                return None
            return {"old": "narrow", "l1": "level-1", "l2": "level-2-only"}.get(
                next(iter(values)), next(iter(values))
            )
        if not re.search(r"^router isis \S+", output, re.M):
            return None
        if setting == "metric":
            if profile is None:
                return _metric_style(output)
            match = re.search(r"^\s*metric-style (\S+)", output, re.M)
            return match[1] if match else "wide"
        if setting == "level":
            match = re.search(r"^\s*is-type (\S+)", output, re.M)
            return match[1] if match else "level-1-2"
        if profile != "iosxr":
            return _hello_authentication(output)
        circuits = {}
        for match in re.finditer(
            r"^ interface (\S+)\n(.*?)(?=^ !|^!|\Z)", output, re.M | re.S
        ):
            interface, block = match.groups()
            if re.search(r"^  passive\s*$", block, re.M):
                continue
            mode = re.search(r"^  hello-password (\S+)", block, re.M)
            circuits[interface] = (
                "md5"
                if mode and mode[1] == "hmac-md5"
                else (mode[1] if mode else "none")
            )
        return circuits or None

    nodes = [*peers, host]

    def mismatch():
        if setting == "auth":
            return (
                bool(observed.get(host))
                and all(mode == "md5" for mode in observed[host].values())
                and all(
                    observed.get(peer)
                    and all(mode == "none" for mode in observed[peer].values())
                    for peer in peers
                )
            )
        target, neighbor = (
            ("narrow", "wide") if setting == "metric" else ("level-1", "level-2-only")
        )
        return observed.get(host) == target and all(
            observed.get(peer) == neighbor for peer in peers
        )

    for node in nodes:
        profile = native_router_profile(problem.net_env, node)

        def check(output, router=node, platform=profile):
            observed[router] = value(output, platform)
            return mismatch() if router == host else observed[router] is not None

        steps.append(
            router_cli(
                problem.net_env,
                node,
                f"Read the IS-IS {setting} configuration on {node}.",
                Check(
                    descriptions[setting]
                    if node == host
                    else "the peer's IS-IS setting is readable",
                    check,
                ),
                frr=vtysh("show running-config"),
                iosxr_command="show running-config router isis",
                routeros_command="/routing/isis/instance/print terse"
                if setting == "metric"
                else "/routing/isis/interface-template/print terse",
                srl_command="info from running network-instance default protocols isis | as json",
                evidence=node == host,
            )
        )
    steps.append(
        router_cli(
            problem.net_env,
            host,
            f"Inspect IS-IS adjacency state on {host}.",
            contains(
                r"\bUp\b|state=up",
                "IS-IS retains an Up adjacency despite incompatible metric encodings",
            )
            if setting == "metric"
            else None,
            frr=vtysh("show isis neighbor"),
            iosxr_command="show isis adjacency",
            routeros_command="/routing/isis/neighbor/print terse",
            srl_command="show network-instance default protocols isis adjacency",
            evidence=setting == "metric",
            attempts=12,
            interval_sec=10,
        )
    )
    return OraclePlaybook(
        f"{host} has an IS-IS {setting} mismatch with its directly connected peers.",
        steps,
    )


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
    SR Linux 24.10 accepts narrow metrics but retains peer routes, so it is
    excluded: the required Up-but-unrouted symptom does not appear.
    https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/is-is.html
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "isis_metric_style_mismatch"
    description = "IS-IS metric style is inconsistent with neighboring routers."
    effect_protocol = "isis"
    TAGS = ["isis"]
    isp_protocol = {"igp": "isis", "bgp_mode": "none", "rpki": False}
    supported_backends = None
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

    def oracle_playbook(self, params: IsisMetricStyleMismatchParams) -> OraclePlaybook:
        return _isis_oracle(self, params.host_name, "metric")

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
    SR Linux level-capability:
    https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/is-is.html
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "isis_circuit_level_mismatch"
    description = "IS-IS level is inconsistent with neighboring routers."
    effect_protocol = "isis"
    TAGS = ["isis"]
    isp_protocol = {"igp": "isis", "bgp_mode": "none", "rpki": False}
    supported_backends = ("kathara", "containerlab")
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

    def oracle_playbook(self, params: IsisCircuitLevelMismatchParams) -> OraclePlaybook:
        return _isis_oracle(self, params.host_name, "level")

    def _set_level(self, host: str, level: str) -> None:
        ops = native_fault_ops(self, host, "set_isis_level")
        if ops is not None:
            ops.set_isis_level(level)
            return
        if getattr(self.net_env, "backend", None) == "containerlab":
            instance = self.runtime.lab_api.srl_protocol_instance(
                host, "isis", state=False
            )
            mode = {_LEVEL1: "L1", _LEVEL2: "L2", "level-1-2": "L1L2"}[level]
            base = (
                f"/network-instance default protocols isis instance {instance['name']}"
            )
            old_number = 2 if level == _LEVEL1 else 1
            paths = [
                base,
                *(
                    f"{base} interface {i['interface-name']}"
                    for i in instance["interface"]
                    if any(
                        row["level-number"] == old_number for row in i.get("level", [])
                    )
                ),
            ]
            commands = [f"delete {path} level {old_number}" for path in paths]
            if level == _LEVEL1:
                flat = self.runtime.lab_api.srl_exec_cli(
                    host, f"info flat from running {base}"
                )
                self._srl_level_config = [
                    line.removeprefix("set ")
                    for line in flat.splitlines()
                    if line.startswith("set /") and " level 2 " in line
                ]
                commands.extend(
                    line.replace(" level 2 ", " level 1 ")
                    for line in self._srl_level_config
                )
            else:
                commands.extend(self._srl_level_config)
            self.runtime.lab_api.srl_configure(
                host,
                *commands,
                f"{base} level-capability {mode}",
            )
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
        if getattr(self.net_env, "backend", None) == "containerlab":
            instance = self.runtime.lab_api.srl_protocol_instance(host, "isis")
            return {"L1": _LEVEL1, "L2": _LEVEL2, "L1L2": "level-1-2"}.get(
                instance["level-capability"]
            )
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        return _is_type(config)

    def inject_fault(self, params: IsisCircuitLevelMismatchParams):
        current = self._level(params.host_name)
        if (
            getattr(self.net_env, "backend", None) == "containerlab"
            and current != _LEVEL2
        ):
            raise RuntimeCapabilityError(
                "SR Linux level mismatch requires a Level-2-only instance."
            )
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


class IsisAuthenticationMismatchParams(BaseModel):
    host_name: str = Field(description="Target router host name.")


class IsisAuthenticationMismatch(ProblemBase):
    """Hello authentication is enabled on one router's circuits only.

    RFC 5304 section 2 defines HMAC-MD5 authentication for IS-IS Hello
    PDUs. Requiring authentication on one side rejects the peer's hellos,
    so adjacencies expire and routes through the router disappear.
    https://www.rfc-editor.org/rfc/rfc5304#section-2
    SR Linux interface authentication:
    https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/is-is.html
    FRR uses interface ``isis password md5``:
    https://docs.frrouting.org/en/stable-10.5/isisd.html
    IOS XR uses interface ``hello-password hmac-md5 clear``:
    https://www.cisco.com/c/en/us/td/docs/routers/xr12000/software/xr12k_r4-1/routing/command/reference/routing_cr41xr12k_chapter3.pdf
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "isis_authentication_mismatch"
    description = "IS-IS hello authentication is inconsistent with neighboring routers."
    effect_protocol = "isis"
    TAGS = ["isis"]
    COMPATIBLE_COLUMNS = frozenset(
        f"{scenario}/isis" for scenario in list_isp_base_scenarios()
    )
    isp_protocol = {"igp": "isis", "bgp_mode": "none", "rpki": False}
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset(
        {"isis_hello_authentication", "set_isis_hello_password"}
    )
    Params = IsisAuthenticationMismatchParams

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        # RouterOS 7.21.5 exposes no hello authentication parameters in its
        # IS-IS interface-template or instance CLI schemas.
        if device_profile == "routeros":
            return False
        return super().runs_on_device_profile(device_profile)

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "isis"
        )

    def root_cause_resources(self, params: IsisAuthenticationMismatchParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(
        self, params: IsisAuthenticationMismatchParams
    ) -> OraclePlaybook:
        return _isis_oracle(self, params.host_name, "auth")

    def _authentication(self, host: str) -> dict[str, str]:
        ops = native_fault_ops(self, host, "isis_hello_authentication")
        if ops is not None:
            return ops.isis_hello_authentication()
        if getattr(self.net_env, "backend", None) == "containerlab":
            return _srl_authentication(
                self.runtime.lab_api.srl_protocol_instance(host, "isis", state=False)
            )
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        return _hello_authentication(config)

    def _set_password(self, host: str, password: str | None) -> None:
        ops = native_fault_ops(self, host, "set_isis_hello_password")
        if ops is not None:
            ops.set_isis_hello_password(password)
            return
        if getattr(self.net_env, "backend", None) == "containerlab":
            instance = self.runtime.lab_api.srl_protocol_instance(
                host, "isis", state=False
            )
            commands = []
            for interface in instance["interface"]:
                if interface.get("passive", False):
                    continue
                path = f"/network-instance default protocols isis instance {instance['name']} interface {interface['interface-name']} authentication"
                commands.extend(
                    [
                        f"{path} key crypto-algorithm hmac-md5",
                        f"{path} key auth-password {password}",
                        f"{path} hello-authentication generate true",
                        f"{path} hello-authentication check-received strict",
                    ]
                    if password
                    else [f"delete {path}"]
                )
            self.runtime.lab_api.srl_configure(host, *commands)
            return
        lines = []
        for interface in self._authentication(host):
            lines.extend(
                [
                    f"interface {interface}",
                    f"isis password md5 {password}" if password else "no isis password",
                    "exit",
                ]
            )
        self.runtime.exec(host, frr_config_command(*lines))
        frr_save_config(self.runtime, host)

    def inject_fault(self, params: IsisAuthenticationMismatchParams):
        from nika.validation.symptom.routing_probes import facing_interfaces

        host = params.host_name
        for router in sorted({host, *facing_interfaces(self, host)}):
            modes = self._authentication(router)
            if not modes or any(mode != "none" for mode in modes.values()):
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} requires unauthenticated IS-IS circuits on {router!r}."
                )
        self._healthy_routing = _routing_state(self, host)
        self._circuits = set(self._authentication(host))
        self._set_password(host, "K7m4P9r2")

    def verify_fault(self, params: IsisAuthenticationMismatchParams) -> dict:
        modes = self._authentication(params.host_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(modes)
            and set(modes) == getattr(self, "_circuits", set(modes))
            and all(mode == "md5" for mode in modes.values()),
            details={"host": params.host_name, "hello_authentication": modes},
        )

    def recover_fault(self, params: IsisAuthenticationMismatchParams) -> dict:
        baseline = getattr(self, "_healthy_routing", None)
        if not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        host = params.host_name
        self._set_password(host, None)
        restored = wait_until(
            lambda: (
                self._authentication(host) == dict.fromkeys(self._circuits, "none")
                and _routing_restored(self, host, baseline)
            ),
            120,
            interval_s=3,
        )
        return {
            "verified": restored,
            "details": {
                "host": host,
                "hello_authentication": self._authentication(host),
            },
        }
