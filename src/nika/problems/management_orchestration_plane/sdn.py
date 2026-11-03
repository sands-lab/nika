"""SDN controller / southbound failure implementations (ONOS)."""

import time
import shlex
import json

from pydantic import BaseModel, Field

from nika.net_env.sdn_l3_clos.fabric_manager import (
    get_openflow_listen_ports,
    set_openflow_listen_ports,
)
from nika.net_env.sdn_l3_clos.fabric_manager.apply import (
    _onos_batch_resilient,
    _onos_get,
    onos_flow_snapshot,
    onos_group_snapshot,
    wait_for_onos,
    wait_for_of_sessions,
)
from nika.problems.rca import node_resource
from nika.problems.support.polling import wait_until
from nika.problems.support.nft_recovery import NftDropRecovery
from nika.problems.support.recovery import checked_exec
from nika.problems.support.benchmark_targets import choice
from nika.problems.support.oracle import (
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    nft_rule,
    onos,
    shell,
)
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


def _benchmark_controller(ctx) -> dict[str, str]:
    """Benchmark target: one SDN controller (falls back to ``host0``)."""
    controller_pool = ctx.roles.get("controllers") or ctx.controllers
    return {"host_name": choice(ctx.rng, controller_pool, ctx.host0)}


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


def _probe_switch(net_env) -> str:
    """Fabric switch whose controller channel the oracle inspects."""
    model = getattr(net_env, "model", None)
    leaves = list(getattr(model, "leaves", None) or [])
    return leaves[0] if leaves else "leaf_1"


def _switch_channel(
    switch: str, purpose: str, expect, *, evidence: bool = False
) -> OracleStep:
    """The switch's controller target, session state, and recent rconn log."""
    return shell(
        switch,
        "ovs-vsctl --columns=target,is_connected list controller; "
        "grep -E 'rconn' /var/log/openvswitch/ovs-vswitchd.log | tail -4",
        purpose,
        expect,
        evidence=evidence,
        attempts=6,
        interval_sec=10,
    )


def _disconnected(port: int):
    return all_of(
        contains(
            rf"tcp:[0-9.]+:{port}\b", f"the switch targets controller port {port}"
        ),
        contains(r"is_connected\s*:\s*false", "the switch has no controller session"),
    )


def _listeners(host: str, ports: str, purpose: str, expect, **kw) -> OracleStep:
    return shell(
        host,
        "if out=$(ss -ltn 2>&1); then "
        f"echo \"$out\" | grep -E ':({ports})\\b' || echo NO_LISTENER; "
        'else echo "ss failed: $out"; fi',
        purpose,
        expect,
        **kw,
    )


_DEVICES_UNAVAILABLE = all_of(
    contains(r'"available":\s*false', "ONOS lists switches as unavailable"),
    lacks(r'"available":\s*true', "no switch is available"),
)


def _active_onos_apps(runtime, *, required: set[str] | None = None) -> set[str]:
    raw = _onos_get(runtime, "/onos/v1/applications")
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Cannot read the controller application state: {raw[:500]}"
        ) from exc
    if not isinstance(snapshot, dict) or not isinstance(
        snapshot.get("applications"), list
    ):
        raise RuntimeError(f"Cannot read the controller application state: {raw[:500]}")
    installed = {app["name"] for app in snapshot["applications"]}
    if required and not required.issubset(installed):
        raise RuntimeError(
            f"Controller application registry is missing {sorted(required - installed)}"
        )
    return {
        app["name"] for app in snapshot["applications"] if app.get("state") == "ACTIVE"
    }


def _restore_onos_apps(runtime, saved: set[str]) -> bool:
    current = set()
    last_error = None

    def ready():
        nonlocal current, last_error
        try:
            current = _active_onos_apps(runtime, required=saved)
        except RuntimeError as exc:
            last_error = str(exc)
            return False
        return True

    if not wait_until(ready, 120, interval_s=2):
        raise RuntimeError(
            f"Controller application state did not become ready: {last_error}"
        )
    operations = [
        ("POST", f"/onos/v1/applications/{name}/active", None)
        for name in sorted(saved - current)
    ]
    operations += [
        ("DELETE", f"/onos/v1/applications/{name}/active", None)
        for name in sorted(current - saved)
    ]
    if operations:
        _onos_batch_resilient(runtime, operations, operation="recover applications")
    return wait_for_onos(runtime, timeout_sec=120, stable_checks=2) and wait_until(
        lambda: _active_onos_apps(runtime, required=saved) == saved, 60, interval_s=2
    )


def _flow_policy(flow: dict) -> dict:
    return {
        key: flow[key]
        for key in (
            "deviceId",
            "tableId",
            "priority",
            "timeout",
            "isPermanent",
            "selector",
            "treatment",
        )
        if key in flow
    }


def _group_policy(group: dict) -> dict:
    raw_id = group.get("givenGroupId", group.get("groupId"))
    if raw_id is None:
        raw_id = group["id"]
    group_id = int(str(raw_id), 0)
    return {
        "type": group["type"],
        "appCookie": group["appCookie"],
        "groupId": str(group_id),
        "buckets": [
            {
                key: bucket[key]
                for key in ("weight", "treatment", "watchPort", "watchGroup")
                if key in bucket
            }
            for bucket in group["buckets"]
        ],
    }


def _saved_rest_policy(runtime) -> tuple[list[dict], list[dict]]:
    # ONOS rebuilds discovery rules; retain the authored REST policy.
    flows = [
        _flow_policy(flow)
        for flow in onos_flow_snapshot(runtime).get("flows", [])
        if flow.get("appId") == "org.onosproject.rest"
    ]
    groups = [
        {"deviceId": group["deviceId"], **_group_policy(group)}
        for group in onos_group_snapshot(runtime).get("groups", [])
        if group.get("appId") == "org.onosproject.rest"
    ]
    return flows, groups


def _rest_policy_present(runtime, saved) -> bool:
    current = _saved_rest_policy(runtime)
    return all(
        {json.dumps(item, sort_keys=True) for item in wanted}.issubset(
            json.dumps(item, sort_keys=True) for item in actual
        )
        for wanted, actual in zip(saved, current, strict=True)
    )


def _restore_rest_policy(runtime, saved) -> bool:
    flows, groups = saved
    current_flows, current_groups = _saved_rest_policy(runtime)
    for kind, wanted, current in (
        ("groups", groups, current_groups),
        ("flows", flows, current_flows),
    ):
        present = {json.dumps(item, sort_keys=True) for item in current}
        operations = []
        for item in wanted:
            if json.dumps(item, sort_keys=True) in present:
                continue
            body = (
                {key: value for key, value in item.items() if key != "deviceId"}
                if kind == "groups"
                else item
            )
            operations.append(("POST", f"/onos/v1/{kind}/{item['deviceId']}", body))
        if operations:
            _onos_batch_resilient(runtime, operations, operation=f"recover {kind}")

    return wait_until(lambda: _rest_policy_present(runtime, saved), 60, interval_s=2)


class SDNControllerCrashParams(BaseModel):
    """Parameters for injecting an SDN controller crash fault."""

    host_name: str = Field(description="Target SDN controller host name.")


class SDNControllerCrash(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "sdn_controller_crash"
    description = "SDN controller is down."
    TAGS: str = ["sdn"]

    Params = SDNControllerCrashParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return _benchmark_controller(ctx)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SDNControllerCrashParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SDNControllerCrashParams):
        self._controller_switch_state = _switch_controller_state(
            self.runtime, self.net_env
        )
        self._controller_was_running = not self.verify_fault(params)["verified"]
        self._controller_policy = (
            _saved_rest_policy(self.runtime)
            if self._controller_was_running
            else ([], [])
        )
        self._controller_apps = (
            _active_onos_apps(self.runtime) if self._controller_was_running else set()
        )
        self._controller_ports = (
            get_openflow_listen_ports(self.runtime)
            if self._controller_was_running
            else None
        )
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

    def oracle_playbook(self, params: SDNControllerCrashParams) -> OraclePlaybook:
        host = params.host_name
        switch = _probe_switch(self.net_env)
        return OraclePlaybook(
            finding=(
                f"The ONOS controller process on {host} is gone, so its "
                "OpenFlow and REST endpoints are closed and no switch has a "
                "controller session."
            ),
            steps=[
                _switch_channel(
                    switch,
                    f"Check the controller session of {switch}.",
                    _disconnected(ONOS_OF_PORT_DEFAULT),
                ),
                onos(
                    "/onos/v1/devices",
                    "Query the controller's device inventory over REST.",
                    contains(r'"summary":\s*\{\}', "the ONOS REST API returns nothing"),
                ),
                shell(
                    host,
                    "if out=$(ps -eo pid,stat,args 2>&1); then "
                    "echo \"$out\" | awk '$2 !~ /^Z/' "
                    "| grep -E '[k]araf|[j]ava' || echo NO_CONTROLLER_PROCESS; "
                    'else echo "ps failed: $out"; fi',
                    f"Look for the ONOS (Karaf/Java) process on {host}.",
                    contains(r"^NO_CONTROLLER_PROCESS$", "no live ONOS process"),
                    evidence=True,
                ),
                _listeners(
                    host,
                    f"{ONOS_OF_PORT_DEFAULT}|{ONOS_OF_PORT_MISMATCH}|8181",
                    f"Check the OpenFlow and REST listeners on {host}.",
                    contains(r"^NO_LISTENER$", "nothing listens on 6653, 6633 or 8181"),
                    evidence=True,
                ),
            ],
        )

    def recover_fault(self, params: SDNControllerCrashParams) -> dict:
        host = params.host_name
        was_running = getattr(self, "_controller_was_running", None)
        if was_running is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        if not was_running:
            return build_verify_result(
                self.root_cause_name,
                self.verify_fault(params)["verified"],
                {"host": host, "original_service_running": False},
            )
        checked_exec(
            self.runtime,
            host,
            "cd /root/onos && if ! pgrep -f '[o]rg.apache.karaf.main.Main' >/dev/null 2>&1; then setsid /root/onos/bin/onos-service server >/tmp/onos-service.log 2>&1 < /dev/null & fi",
        )

        if not wait_for_onos(self.runtime, timeout_sec=180, stable_checks=2):
            return build_verify_result(
                self.root_cause_name,
                False,
                {"host": host, "error": "controller_rest_unavailable"},
            )
        if not _restore_onos_apps(self.runtime, self._controller_apps):
            return build_verify_result(
                self.root_cause_name,
                False,
                {"host": host, "error": "controller_apps_not_restored"},
            )
        policy_restored = not any(self._controller_policy) or _rest_policy_present(
            self.runtime, self._controller_policy
        )
        policy_error = None
        if not policy_restored:
            # Rediscover devices before replaying their policy, then restore the listener mismatch.
            target_ports = {
                int(info["target"].rpartition(":")[2])
                for info in self._controller_switch_state.values()
                if info["target"].rpartition(":")[2].isdigit()
            }
            try:
                if self._controller_ports is not None:
                    set_openflow_listen_ports(
                        self.runtime, sorted(target_ports | set(self._controller_ports))
                    )
                if wait_for_of_sessions(
                    self.runtime, getattr(self.net_env, "model", None), timeout_sec=120
                ):
                    policy_restored = _restore_rest_policy(
                        self.runtime, self._controller_policy
                    )
                else:
                    policy_error = "controller_devices_not_discovered"
            except Exception as exc:  # noqa: BLE001
                policy_error = str(exc)
        if self._controller_ports is not None:
            # Restore temporary listeners before any failed readback can return.
            set_openflow_listen_ports(self.runtime, self._controller_ports)
            refused_ports = {
                int(previous["target"].rpartition(":")[2])
                for previous in self._controller_switch_state.values()
                if previous["is_connected"] == "false"
                and previous["target"].rpartition(":")[2].isdigit()
                and int(previous["target"].rpartition(":")[2])
                not in self._controller_ports
            }
            if not wait_until(
                lambda: (
                    get_openflow_listen_ports(self.runtime) == self._controller_ports
                    and all(
                        not _tcp_listening(self.runtime, host, port)
                        for port in refused_ports
                    )
                ),
                60,
                interval_s=2,
            ):
                return build_verify_result(
                    self.root_cause_name,
                    False,
                    {"host": host, "error": "controller_listeners_not_restored"},
                )
        # A restarted ONOS can briefly accept a previously mismatched switch.
        # Reset those sessions after restoring the original listeners.
        current = _switch_controller_state(self.runtime, self.net_env)
        for switch, previous in self._controller_switch_state.items():
            if (
                previous["is_connected"] == "false"
                and current.get(switch, {}).get("is_connected") == "true"
            ):
                target = shlex.quote(previous["target"])
                checked_exec(
                    self.runtime,
                    switch,
                    f"for br in $(ovs-vsctl list-br); do ovs-vsctl set-controller $br tcp:127.0.0.1:1 && ovs-vsctl set-controller $br {target} || exit; done",
                )

        def connected():
            state = _switch_controller_state(self.runtime, self.net_env)
            return (
                _tcp_listening(self.runtime, host, 8181)
                and all(
                    _tcp_listening(self.runtime, host, port)
                    for port in (self._controller_ports or [])
                )
                and all(
                    state.get(name, {}).get("is_connected") == previous["is_connected"]
                    for name, previous in self._controller_switch_state.items()
                )
            )

        channels_restored = wait_until(connected, 180, interval_s=2)
        return build_verify_result(
            self.root_cause_name,
            channels_restored and policy_restored,
            {
                "host": host,
                "controller_channels_restored": channels_restored,
                "controller_policy_restored": policy_restored,
                "policy_error": policy_error,
            },
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
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "southbound_port_block"
    description = "Controller southbound channel port is blocked."
    TAGS: str = ["sdn"]

    Params = SouthboundPortBlockParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = _benchmark_controller(ctx)
        params["southbound_port"] = "6653"
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SouthboundPortBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortBlockParams):
        self._southbound_switch_state = _switch_controller_state(
            self.runtime, self.net_env
        )
        self._southbound_recovery = NftDropRecovery()
        self._southbound_recovery.add(
            self.runtime,
            params.host_name,
            _southbound_drop_rule(params.southbound_port),
        )

    def oracle_playbook(self, params: SouthboundPortBlockParams) -> OraclePlaybook:
        host, port = params.host_name, int(params.southbound_port)
        switch = _probe_switch(self.net_env)
        return OraclePlaybook(
            finding=(
                f"A firewall rule on {host} drops TCP port {port}, so switches "
                "cannot reach the running ONOS OpenFlow listener."
            ),
            steps=[
                _switch_channel(
                    switch,
                    f"Check the controller session of {switch}.",
                    _disconnected(port),
                ),
                OracleStep(
                    "List the switches ONOS knows and their availability.",
                    "sdn_onos_rest",
                    {"path": "/onos/v1/devices"},
                    _DEVICES_UNAVAILABLE,
                    evidence=True,
                    attempts=6,
                    interval_sec=10,
                ),
                _listeners(
                    host,
                    str(port),
                    f"Confirm ONOS still listens on TCP {port}.",
                    contains(rf"LISTEN.*:{port}\b", f"ONOS listens on {port}"),
                    evidence=True,
                ),
                nft_rule(
                    host,
                    rf"tcp dport {port} (drop|reject)",
                    f"Inspect the packet filter on {host}.",
                ),
            ],
        )

    def recover_fault(self, params: SouthboundPortBlockParams) -> dict:
        recovery = getattr(self, "_southbound_recovery", None)
        if recovery is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        rules_removed = recovery.restore(self.runtime)
        restored = rules_removed
        originally_connected = {
            name
            for name, info in self._southbound_switch_state.items()
            if info["is_connected"] == "true"
        }
        if restored and originally_connected:

            def channels_restored():
                state = _switch_controller_state(self.runtime, self.net_env)
                return all(
                    state.get(name, {}).get("is_connected") == "true"
                    for name in originally_connected
                )

            restored = wait_until(channels_restored, 90, interval_s=2)
        return build_verify_result(
            self.root_cause_name,
            restored,
            {
                "host": params.host_name,
                "owned_rules_removed": rules_removed,
                "controller_channels_restored": restored,
            },
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
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "southbound_port_mismatch"
    description = "Controller southbound listen port mismatches switch config."
    TAGS: str = ["sdn"]

    Params = SouthboundPortMismatchParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = _benchmark_controller(ctx)
        params["mismatched_port"] = "6633"
        params["original_port"] = "6653"
        return params

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
        self._original_switch_state = _switch_controller_state(
            self.runtime, self.net_env
        )
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

    def oracle_playbook(self, params: SouthboundPortMismatchParams) -> OraclePlaybook:
        host = params.host_name
        configured, listening = int(params.original_port), int(params.mismatched_port)
        switch = _probe_switch(self.net_env)
        return OraclePlaybook(
            finding=(
                f"ONOS on {host} listens for OpenFlow only on {listening} while "
                f"the switches still connect to {configured}, so every switch "
                "is refused."
            ),
            steps=[
                _switch_channel(
                    switch,
                    f"Check which controller port {switch} targets and its session.",
                    _disconnected(configured),
                    evidence=True,
                ),
                _listeners(
                    host,
                    f"{configured}|{listening}",
                    f"Compare the OpenFlow listeners on {host}.",
                    all_of(
                        contains(
                            rf"LISTEN.*:{listening}\b", f"ONOS listens on {listening}"
                        ),
                        lacks(rf":{configured}\b", f"nothing listens on {configured}"),
                    ),
                    evidence=True,
                ),
                onos(
                    "/onos/v1/configuration/"
                    "org.onosproject.openflow.controller.impl.OpenFlowControllerImpl",
                    "Read the ONOS OpenFlow controller configuration.",
                    contains(
                        rf'"openflowPorts":\s*"{listening}"',
                        f"openflowPorts is set to {listening} only",
                    ),
                    evidence=True,
                ),
                shell(
                    host,
                    "if rules=$(nft list ruleset 2>&1); then "
                    "echo \"$rules\" | grep -E 'dport' || echo NO_PORT_RULES; "
                    'else echo "nft failed: $rules"; fi',
                    f"Rule out a port filter on {host}.",
                    all_of(
                        contains(r"dport|^NO_PORT_RULES$", "the ruleset was read"),
                        lacks(r"nft failed", "nft ran"),
                        lacks(
                            rf"dport {configured}\b.*(drop|reject)",
                            f"no filter drops port {configured}",
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )

    def recover_fault(self, params: SouthboundPortMismatchParams) -> dict:
        ports = getattr(self, "_original_listen_ports", None)
        if ports is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        set_openflow_listen_ports(self.runtime, ports)

        def connected():
            state = _switch_controller_state(self.runtime, self.net_env)
            return (
                get_openflow_listen_ports(self.runtime) == ports
                and all(
                    _tcp_listening(self.runtime, params.host_name, port)
                    for port in ports
                )
                and bool(state)
                and all(
                    state.get(name, {}).get("is_connected") == "true"
                    for name, original in self._original_switch_state.items()
                    if original["is_connected"] == "true"
                )
            )

        restored = wait_until(connected, 90, interval_s=2)
        return build_verify_result(
            self.root_cause_name,
            restored,
            {
                "host": params.host_name,
                "openflow_ports": ports,
                "controller_channels_restored": restored,
            },
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
        # The artifact is the listener config and the switch targets it no
        # longer matches. Whether switches are connected is the symptom.
        state, all_target_original, _ = self._switch_targets_original(params)
        verified = (
            config_mismatched
            and not original_listening
            and mismatched_listening
            and all_target_original
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "openflow_ports": ports,
                "original_port_listening": original_listening,
                "mismatched_port_listening": mismatched_listening,
                "switch_targets": {
                    switch: item["target"] for switch, item in state.items()
                },
            },
        )
