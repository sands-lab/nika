from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.routing_control_plane.ospf import linked_routers
from nika.problems.support.oracle import (
    OraclePlaybook,
    contains,
    process_absent,
    router_cli,
    shell,
    vtysh,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.recovery import checked_exec
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.utils.logger import system_logger

logger = system_logger

# ==================================================================
# Problem: routing service down on a router device
# ==================================================================


class RoutingServiceDownParams(BaseModel):
    """Target for a routing service outage."""

    host_name: str = Field(description="Target router host name.")


class RoutingServiceDown(ProblemBase):
    """The complete routing service on a router is stopped."""

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "routing_service_down"
    description = "The router's routing service is unavailable."
    TAGS = []
    native_router_operations = frozenset(
        {"stop_routing_process", "start_routing_process", "routing_process_status"}
    )

    Params = RoutingServiceDownParams

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        from nika.net_env.net_env_pool import effective_tags
        from nika.problems.support.router_ops import router_platform_has_provider

        if not super().is_compatible(target, device_profile, backend=backend):
            return False
        tags = effective_tags(target)
        return "frr" in tags or any(
            cls.runs_on_device_profile(profile)
            for profile in {*tags, device_profile}
            if router_platform_has_provider(profile)
        )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def benchmark_node_targets(cls, ctx, targets):
        # A service outage applies across router roles, including native cores
        # in mixed deployments. Static Linux routers have no routing service.
        identities = getattr(ctx.net_env, "machine_identities", None) or {}
        return sorted(
            node
            for node in ctx.net_env.routers
            if "frr"
            in (identity.capabilities if (identity := identities.get(node)) else ())
            or native_router_profile(ctx.net_env, node) is not None
        )

    symptom_desc = "Users report connectivity issues to other hosts in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: RoutingServiceDownParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(self, params: RoutingServiceDownParams) -> OraclePlaybook:
        host = params.host_name
        steps = [
            shell(
                host,
                "ip -br link; ip route | head -40",
                f"List {host}'s interfaces and the routes it still has.",
            ),
            process_absent(
                host,
                "zebra|bgpd|ospfd|ospf6d|isisd|staticd|watchfrr",
                f"Check whether FRR's daemons run on {host}.",
            ),
            shell(
                host,
                vtysh("show ip route") + " 2>&1 | head -5",
                f"Query FRR on {host} through vtysh.",
                contains(
                    r"failed to connect|is not running|Connection refused",
                    "vtysh cannot reach the routing daemons",
                ),
                evidence=True,
            ),
        ]
        for _intf, peer, _peer_intf in linked_routers(self.net_env, host)[:1]:
            steps.append(
                router_cli(
                    self.net_env,
                    peer,
                    f"Check neighbor {peer}'s routing adjacencies toward {host}.",
                    iosxr_command="show bgp summary",
                    routeros_command="/routing/bgp/session/print detail",
                    srl_command="show network-instance default protocols bgp neighbor",
                    frr="; ".join(
                        vtysh(show) + " 2>&1 | head -15"
                        for show in (
                            "show ip ospf neighbor",
                            "show isis neighbor",
                            "show bgp summary",
                        )
                    ),
                )
            )
        return OraclePlaybook(
            finding=(
                f"The routing service on {host} is stopped, so it holds "
                "no routing adjacencies and stops exchanging routes."
            ),
            steps=steps,
        )

    def inject_fault(self, params: RoutingServiceDownParams):
        ops = native_fault_ops(self, params.host_name, "stop_routing_process")
        if ops is not None:
            self._routing_ops = ops
            self._routing_processes = ops.routing_process_status()
            if self._routing_processes.get("zebra"):
                self._routing_peers = ops.bgp_established_peers()
                self._routing_bgp_routes = ops.remote_bgp_prefixes()
                self._routing_ospf_routes = ops.ospf_routes()
            ops.stop_routing_process()
            return
        self._routing_was_running = bool(
            self.runtime.exec(
                params.host_name, "pgrep -x zebra 2>/dev/null || true"
            ).strip()
        )
        # systemctl is a no-op in Kathara; kill FRR daemons directly with pkill.
        # watchfrr must be killed first so it does not restart the routing daemons.
        for daemon in (
            "watchfrr",
            "zebra",
            "mgmtd",
            "ospfd",
            "bgpd",
            "staticd",
            "ospf6d",
            "ripd",
            "ripngd",
            "isisd",
            "fabricd",
            "ldpd",
            "eigrpd",
            "bfdd",
            "pathd",
            "vrrpd",
            "pimd",
            "pim6d",
        ):
            self.runtime.kill_process(params.host_name, daemon)

    def recover_fault(self, params: RoutingServiceDownParams) -> dict:
        ops = getattr(self, "_routing_ops", None)
        if ops is not None:
            saved = self._routing_processes
            details = {"host": params.host_name, "routing_available": False}
            try:
                ops.start_routing_process(saved)
            except Exception as exc:
                return build_verify_result(
                    self.root_cause_name, False, {**details, "error": str(exc)}
                )

            def restored():
                status = ops.routing_process_status()
                details["routing_processes"] = status
                if status != saved:
                    return False
                if not saved.get("zebra"):
                    return True
                output = ops.frr_shell("vtysh -c 'show ip route' 2>&1")
                available = bool(output.strip()) and not any(
                    error in output.lower()
                    for error in (
                        "failed to connect",
                        "not running",
                        "connection refused",
                        "%",
                        "[timeout]",
                    )
                )
                details["routing_available"] = available
                converged = (
                    ops.bgp_established_peers() == self._routing_peers
                    and ops.remote_bgp_prefixes() == self._routing_bgp_routes
                    and ops.ospf_routes() == self._routing_ospf_routes
                )
                details["routing_converged"] = converged
                return available and converged

            verified = wait_until(restored, 90)
            return build_verify_result(self.root_cause_name, verified, details)
        was_running = getattr(self, "_routing_was_running", None)
        if was_running is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        if not was_running:
            return build_verify_result(
                self.root_cause_name,
                not bool(
                    self.runtime.exec(
                        params.host_name, "pgrep -x zebra 2>/dev/null || true"
                    ).strip()
                ),
                {"host": params.host_name, "original_service_running": False},
            )
        # Native targets without this operation already reject injection.
        native_fault_ops(self, params.host_name, "stop_routing_process")
        zebra = self.runtime.exec(
            params.host_name, "pgrep -x zebra 2>/dev/null || true"
        ).strip()
        if not zebra:
            checked_exec(
                self.runtime, params.host_name, "/usr/lib/frr/frrinit.sh start"
            )

        def running():
            output = self.runtime.exec(
                params.host_name, "vtysh -c 'show ip route' 2>&1"
            )
            return bool(output.strip()) and not any(
                error in output.lower()
                for error in ("failed to connect", "not running", "connection refused")
            )

        restored = wait_until(running, 60)
        return build_verify_result(
            self.root_cause_name,
            restored,
            {"host": params.host_name, "routing_available": restored},
        )

    def verify_fault(self, params: RoutingServiceDownParams) -> dict:
        """Verify the router's routing processes are stopped."""
        ops = native_fault_ops(self, params.host_name, "routing_process_status")
        if ops is not None:
            status = ops.routing_process_status()
            return build_verify_result(
                self.root_cause_name,
                bool(status) and not any(status.values()),
                {"host": params.host_name, "routing_processes": status},
            )
        zebra_output = self.runtime.exec(
            params.host_name, "pgrep -a zebra 2>/dev/null || echo NONE"
        ).strip()
        # show version still succeeds in FRR 9.x when zebra is down; use show ip route instead.
        vtysh_output = self.runtime.exec(
            params.host_name, "vtysh -c 'show ip route' 2>&1 | head -3"
        ).strip()
        zebra_down = zebra_output == "NONE" or "zebra" not in zebra_output
        routing_unavailable = (
            "failed to connect" in vtysh_output.lower()
            or "not running" in vtysh_output.lower()
        )
        verified = zebra_down and routing_unavailable
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "zebra_output": zebra_output,
                "vtysh_output": vtysh_output,
                "zebra_down": zebra_down,
                "routing_unavailable": routing_unavailable,
            },
        )
