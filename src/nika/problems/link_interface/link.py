import shlex

from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    build_verify_result,
    ProblemBase,
)
from nika.problems.topology_inventory import interface_on
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger


def _default_link_intf(backend: str) -> str:
    return "e1-1" if backend == "containerlab" else "eth0"


def _resolve_link_intf(params_intf: str, backend: str) -> str:
    if params_intf != "eth0":
        return params_intf
    return _default_link_intf(backend)


# ==================================================================
# Problem: Link failure by ip link down on host interface
# ==================================================================


class LinkFailureParams(BaseModel):
    """Parameters for injecting a link-down fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class LinkFailure(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    cause = FailureCause.HARDWARE
    symptom = FailureSymptom.DOWN
    scope = FailureScope.LINK
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "link_down"
    TAGS: str = ["link"]

    Params = LinkFailureParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"
        self.down_time = 1
        self.up_time = 1

    def root_cause_resources(self, params: LinkFailureParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkFailureParams):
        match self.lab_backend:
            case "kathara":
                self._inject_link_down_kathara(params)
            case "containerlab":
                self._inject_link_down_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_link_down_kathara(self, params: LinkFailureParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self.faulty_intf = intf
        self.runtime.set_interface_state(params.host_name, intf, "down")

    def _inject_link_down_containerlab(self, params: LinkFailureParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self.faulty_intf = intf
        self.runtime.set_interface_state(params.host_name, intf, "down")

    def verify_fault(self, params: LinkFailureParams) -> dict:
        """Verify the link-down fault is active by reading the interface operstate from the container."""
        match self.lab_backend:
            case "kathara":
                return self._verify_link_down_kathara(params)
            case "containerlab":
                return self._verify_link_down_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_link_down_kathara(self, params: LinkFailureParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        return self._verify_link_down(params, intf)

    def _verify_link_down_containerlab(self, params: LinkFailureParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        return self._verify_link_down(params, intf)

    def _verify_link_down(self, params: LinkFailureParams, intf: str) -> dict:
        operstate = self.runtime.get_interface_operstate(params.host_name, intf)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=operstate == "down",
            details={
                "host": params.host_name,
                "intf": intf,
                "operstate": operstate,
            },
        )


# ==========================================
# Problem: Link flapping by manual script
# ==========================================


class LinkFlapParams(BaseModel):
    """Parameters for injecting a link-flap fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")
    down_time: int = Field(default=1, description="Down duration in seconds.")
    up_time: int = Field(default=1, description="Up duration in seconds.")


class LinkFlap(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    cause = FailureCause.HARDWARE
    symptom = FailureSymptom.FLAP
    scope = FailureScope.LINK
    temporal = FailureTemporal.INTERMITTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "link_flap"
    TAGS: str = ["link"]

    Params = LinkFlapParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkFlapParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkFlapParams):
        match self.lab_backend:
            case "kathara":
                self._inject_link_flap_kathara(params)
            case "containerlab":
                self._inject_link_flap_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_link_flap_kathara(self, params: LinkFlapParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self._inject_link_flap(params, intf)

    def _inject_link_flap_containerlab(self, params: LinkFlapParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self._inject_link_flap(params, intf)

    def _inject_link_flap(self, params: LinkFlapParams, intf_name: str) -> None:
        self.faulty_intf = intf_name
        if params.down_time <= 0 or params.up_time <= 0:
            raise ValueError("down_time and up_time must be positive integers")

        script_path = f"/tmp/link_flap_{intf_name}.sh"
        pid_path = f"/tmp/link_flap_{intf_name}.pid"
        log_path = f"/tmp/link_flap_{intf_name}.log"

        script = f"""#!/bin/bash
IFACE={shlex.quote(intf_name)}
DOWN_TIME={int(params.down_time)}
UP_TIME={int(params.up_time)}
PID_FILE={shlex.quote(pid_path)}

cleanup() {{
    ip link set $IFACE up >/dev/null 2>&1 || true
    rm -f $PID_FILE
}}
trap cleanup EXIT INT TERM

echo $$ > $PID_FILE
while true; do
    ip link set $IFACE down
    sleep $DOWN_TIME
    ip link set $IFACE up
    sleep $UP_TIME
done
"""
        write_cmd = f"cat <<'EOF' > {shlex.quote(script_path)}\n{script}\nEOF\nchmod +x {shlex.quote(script_path)}"
        self.runtime.exec(params.host_name, write_cmd)

        stop_previous_cmd = (
            f"if [ -f {shlex.quote(pid_path)} ]; then "
            f"kill $(cat {shlex.quote(pid_path)}) 2>/dev/null || true; "
            f"rm -f {shlex.quote(pid_path)}; "
            "fi"
        )
        self.runtime.exec(params.host_name, stop_previous_cmd)

        start_cmd = f"nohup {shlex.quote(script_path)} > {shlex.quote(log_path)} 2>&1 < /dev/null &"
        self.runtime.exec(params.host_name, start_cmd)
        system_logger.info(
            f"Injected link flap on {params.host_name}:{intf_name} "
            f"(down_time={params.down_time}, up_time={params.up_time})"
        )

    def verify_fault(self, params: LinkFlapParams) -> dict:
        """Verify the link-flap script is running by checking the pid file and process liveness."""
        match self.lab_backend:
            case "kathara":
                return self._verify_link_flap_kathara(params)
            case "containerlab":
                return self._verify_link_flap_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_link_flap_kathara(self, params: LinkFlapParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        return self._verify_link_flap(params, intf)

    def _verify_link_flap_containerlab(self, params: LinkFlapParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        return self._verify_link_flap(params, intf)

    def _verify_link_flap(self, params: LinkFlapParams, intf_name: str) -> dict:
        pid_path = f"/tmp/link_flap_{intf_name}.pid"
        running = self.runtime.pidfile_running(params.host_name, pid_path)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=running,
            details={
                "host": params.host_name,
                "intf": intf_name,
                "flap_process": "running" if running else "not_running",
            },
        )


# ==========================================
# Problem: Link detached.
# ==========================================


class LinkDetachParams(BaseModel):
    """Parameters for injecting a link-detach fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class LinkDetach(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    cause = FailureCause.OPERATIONAL
    symptom = FailureSymptom.DOWN
    scope = FailureScope.LINK
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "link_detach"
    TAGS: str = ["link"]

    Params = LinkDetachParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkDetachParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkDetachParams):
        match self.lab_backend:
            case "kathara":
                self._inject_link_detach_kathara(params)
            case "containerlab":
                self._inject_link_detach_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_link_detach_kathara(self, params: LinkDetachParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self._inject_link_detach(params, intf)

    def _inject_link_detach_containerlab(self, params: LinkDetachParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self._inject_link_detach(params, intf)

    def _inject_link_detach(self, params: LinkDetachParams, intf_name: str) -> None:
        self.faulty_intf = intf_name
        self.runtime.exec(params.host_name, f"ip link del {intf_name}")
        system_logger.info(f"Injected link detach on {params.host_name}:{intf_name}")

    def verify_fault(self, params: LinkDetachParams) -> dict:
        """Verify the link-detach fault is active by confirming the interface no longer exists."""
        match self.lab_backend:
            case "kathara":
                return self._verify_link_detach_kathara(params)
            case "containerlab":
                return self._verify_link_detach_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_link_detach_kathara(self, params: LinkDetachParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        return self._verify_link_detach(params, intf)

    def _verify_link_detach_containerlab(self, params: LinkDetachParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        return self._verify_link_detach(params, intf)

    def _verify_link_detach(self, params: LinkDetachParams, intf_name: str) -> dict:
        detached = not self.runtime.interface_exists(params.host_name, intf_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=detached,
            details={
                "host": params.host_name,
                "intf": intf_name,
                "interface_exists": not detached,
            },
        )
