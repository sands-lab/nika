"""SDN controller / southbound failure implementations (ONOS)."""

import time

from pydantic import BaseModel, Field

from nika.net_env.sdn_l3_clos.fabric_manager import (
    get_openflow_listen_ports,
    set_openflow_listen_ports,
)
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.service.lab.nft_api import (
    nft_chain_has_rule,
    nft_drop_chains,
    nft_list_chain_command,
)
from nika.utils.logger import system_logger

logger = system_logger

ONOS_OF_PORT_DEFAULT = 6653
ONOS_OF_PORT_MISMATCH = 6633
_DROP_TABLE = "filter"
_DISCONNECT_WAIT_SEC = 30.0


def _southbound_drop_rule(port: int) -> str:
    return f"tcp dport {int(port)} drop"


def _southbound_drop_present(runtime, host: str, port: int) -> bool:
    """True when the controller drops ``port`` in every hooked filter chain."""
    rule = _southbound_drop_rule(port)
    return all(
        nft_chain_has_rule(
            runtime.exec(host, nft_list_chain_command(_DROP_TABLE, chain)), rule
        )
        for chain in nft_drop_chains("inet")
    )


def _tcp_listening(runtime, host: str, port: int) -> bool:
    """True when ``host`` has a TCP socket in LISTEN on ``port`` (any address)."""
    # /proc works on images without ss/netstat; state 0A is LISTEN.
    out = runtime.exec(
        host,
        f"awk 'NR>1 && $4==\"0A\" && $2 ~ /:{int(port):04X}$/' "
        "/proc/net/tcp /proc/net/tcp6 2>/dev/null | head -1",
    ).strip()
    return bool(out)


def _switch_controller_state(runtime, net_env) -> dict[str, dict[str, str]]:
    """Return each OVS bridge's controller target and is_connected flag."""
    model = getattr(net_env, "model", None)
    switches = list(getattr(model, "leaves", []) or []) + list(
        getattr(model, "spines", []) or []
    )
    state: dict[str, dict[str, str]] = {}
    for switch in switches:
        raw = runtime.exec(
            switch,
            "ovs-vsctl --format=csv --no-headings --columns=target,is_connected "
            "list controller 2>/dev/null || true",
        ).strip()
        target, _, connected = (
            raw.splitlines()[0].rpartition(",") if raw else ("", "", "")
        )
        state[switch] = {
            "target": target.strip().strip('"'),
            "is_connected": connected.strip(),
        }
    return state


class SDNControllerCrashParams(BaseModel):
    """Parameters for injecting an SDN controller crash fault."""

    host_name: str = Field(description="Target SDN controller host name.")


class SDNControllerCrash(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name: str = "sdn_controller_crash"
    description = "SDN controller is down."
    TAGS: str = ["sdn"]

    Params = SDNControllerCrashParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SDNControllerCrashParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SDNControllerCrashParams):
        # ONOS: JVM plus the karaf wrapper (/bin/sh .../bin/karaf server).
        # PID 1 is a keeper (sleep infinity); SIGKILL leaves exec for verify.
        # Bracket patterns avoid pkill matching the Kathara/docker exec shell
        # argv (which embeds this command string and would suicide before kill).
        self.runtime.exec(
            params.host_name,
            "pkill -9 -f '[o]rg.apache.karaf' 2>/dev/null || true; "
            "pkill -9 -f '[o]nos-service' 2>/dev/null || true; "
            "pkill -9 -f '[j]ava.*karaf' 2>/dev/null || true; "
            "pkill -9 -f '[a]pache-karaf' 2>/dev/null || true; "
            "pkill -9 -f '[/]bin/karaf' 2>/dev/null || true; "
            "pkill -9 -f '[k]araf server' 2>/dev/null || true; "
            "pkill -9 -f '[p]ox.py' 2>/dev/null || true; "
            "sleep 3",
        )

    def verify_fault(self, params: SDNControllerCrashParams) -> dict:
        try:
            pgrep_output = self.runtime.exec(
                params.host_name,
                "pgrep -af 'onos-service|karaf|java.*onos|pox.py' 2>/dev/null "
                "| grep -v 'pgrep\\|bash\\|grep\\|onos-entrypoint\\|sleep infinity' "
                "| grep -v '<defunct>' "
                "| grep . || echo NONE",
            ).strip()
        except Exception as exc:  # noqa: BLE001
            # Container exited with the controller process (legacy images).
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=True,
                details={
                    "host": params.host_name,
                    "pgrep_output": f"exec_failed: {exc}",
                },
            )
        verified = pgrep_output == "NONE" or (
            "onos" not in pgrep_output
            and "karaf" not in pgrep_output
            and "pox" not in pgrep_output
            and "java" not in pgrep_output
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )


class SouthboundPortBlockParams(BaseModel):
    """Parameters for injecting a southbound port block fault."""

    host_name: str = Field(description="Target SDN controller host name.")
    southbound_port: int = Field(
        default=ONOS_OF_PORT_DEFAULT, description="Port to block."
    )


class SouthboundPortBlock(ProblemBase):
    """A firewall on the controller drops its OpenFlow port.

    ONOS keeps listening on the port; switch connection attempts time out.
    """

    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name: str = "southbound_port_block"
    description = "Controller southbound channel port is blocked."
    TAGS: str = ["sdn"]

    Params = SouthboundPortBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SouthboundPortBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name,
            _southbound_drop_rule(params.southbound_port),
            table=_DROP_TABLE,
        )

    def verify_fault(self, params: SouthboundPortBlockParams) -> dict:
        blocked = _southbound_drop_present(
            self.runtime, params.host_name, params.southbound_port
        )
        listening = _tcp_listening(
            self.runtime, params.host_name, params.southbound_port
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=blocked and listening,
            details={
                "host": params.host_name,
                "port": params.southbound_port,
                "drop_rule_present": blocked,
                "controller_listening": listening,
            },
        )


class SouthboundPortMismatchParams(BaseModel):
    """Parameters for injecting a southbound port mismatch fault."""

    host_name: str = Field(description="Target SDN controller host name.")
    mismatched_port: int = Field(
        default=ONOS_OF_PORT_MISMATCH,
        description="Only OpenFlow port the controller listens on after the change.",
    )
    original_port: int = Field(
        default=ONOS_OF_PORT_DEFAULT,
        description="OpenFlow port the switches keep targeting.",
    )


class SouthboundPortMismatch(ProblemBase):
    """The controller's OpenFlow listener no longer matches the switch config.

    ONOS ``openflowPorts`` is changed to ``mismatched_port`` only. Switches
    still target ``original_port`` and are refused. No firewall rule is added.
    """

    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name: str = "southbound_port_mismatch"
    description = "Controller southbound listen port mismatches switch config."
    TAGS: str = ["sdn"]

    Params = SouthboundPortMismatchParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_listen_ports: list[int] | None = None

    def root_cause_resources(self, params: SouthboundPortMismatchParams):
        return [node_resource(params.host_name)]

    def _switch_targets_original(
        self, params: SouthboundPortMismatchParams
    ) -> tuple[dict[str, dict[str, str]], bool, bool]:
        state = _switch_controller_state(self.runtime, self.net_env)
        suffix = f":{params.original_port}"
        targeting = [s for s in state.values() if s["target"].endswith(suffix)]
        all_target_original = bool(state) and len(targeting) == len(state)
        none_connected = all(s["is_connected"] != "true" for s in targeting)
        return state, all_target_original, none_connected

    def _wait_disconnected(self, params: SouthboundPortMismatchParams) -> bool:
        deadline = time.monotonic() + _DISCONNECT_WAIT_SEC
        while time.monotonic() < deadline:
            _, _, none_connected = self._switch_targets_original(params)
            if none_connected and not _tcp_listening(
                self.runtime, params.host_name, params.original_port
            ):
                return True
            time.sleep(2.0)
        return False

    def _reset_stale_switch_sessions(self, params: SouthboundPortMismatchParams):
        """Drop sessions a switch opened while ONOS was restarting listeners.

        ONOS briefly re-binds ``original_port`` while applying the new
        ``openflowPorts``; switches that reconnect in that window keep an
        accepted channel after the listener is gone. Point each bridge at a
        refused placeholder and back to its unchanged target so the next
        connect attempt hits the refused port, as a steady-state mismatch
        would. OVS flushes all flows when a bridge is left with no controller,
        so the controller set never becomes empty.
        """
        state = _switch_controller_state(self.runtime, self.net_env)
        suffix = f":{params.original_port}"
        for switch, info in state.items():
            if info["is_connected"] != "true" or not info["target"].endswith(suffix):
                continue
            target = info["target"]
            self.runtime.exec(
                switch,
                f"for br in $(ovs-vsctl list-br); do "
                f"ovs-vsctl set-controller $br tcp:127.0.0.1:{params.original_port} "
                f"&& ovs-vsctl set-controller $br {target}; "
                f"done",
            )

    def inject_fault(self, params: SouthboundPortMismatchParams):
        if params.mismatched_port == params.original_port:
            raise ValueError("mismatched_port must differ from original_port")
        self._original_listen_ports = get_openflow_listen_ports(self.runtime)
        set_openflow_listen_ports(self.runtime, [params.mismatched_port])
        # ONOS restarts its listeners; wait until switches drop their sessions.
        if not self._wait_disconnected(params):
            self._reset_stale_switch_sessions(params)
            self._wait_disconnected(params)
        logger.info(
            "Set ONOS openflowPorts on %s from %s to %s",
            params.host_name,
            self._original_listen_ports,
            params.mismatched_port,
        )

    def verify_fault(self, params: SouthboundPortMismatchParams) -> dict:
        ports = get_openflow_listen_ports(self.runtime)
        config_mismatched = ports is not None and params.original_port not in ports
        original_listening = _tcp_listening(
            self.runtime, params.host_name, params.original_port
        )
        mismatched_listening = _tcp_listening(
            self.runtime, params.host_name, params.mismatched_port
        )
        state, all_target_original, none_connected = self._switch_targets_original(
            params
        )
        verified = (
            config_mismatched
            and not original_listening
            and mismatched_listening
            and all_target_original
            and none_connected
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "openflow_ports": ports,
                "original_port_listening": original_listening,
                "mismatched_port_listening": mismatched_listening,
                "switch_controllers": state,
            },
        )
