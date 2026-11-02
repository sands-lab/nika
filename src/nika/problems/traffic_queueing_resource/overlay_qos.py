"""Undersized EF bandwidth on an enterprise overlay egress."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, Field

from nika.net_env.enterprise_branch.topology import (
    overlay_qos_for,
    routeros_rate,
    tc_rate,
)
from nika.net_env.native_routers import platform_rate_limit_kbps
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import workload_exercises
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    contains,
    router_cli,
    shell,
)
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.traffic.workload import REALTIME_BULK_CONTENTION


class OverlayQosEfUndersizedParams(BaseModel):
    host_name: str = Field(description="Site Edge whose overlay egress policy changes.")
    intf_name: str = Field(description="WireGuard egress carrying the video call.")
    src_host: str = Field(description="CORP host sending the video call.")
    dst_host: str = Field(description="CORP host receiving the video call.")


def _targets(cls, net_env):
    return [
        target
        for target in net_env.dscp_remark_targets()
        if workload_exercises(
            cls, net_env, {"host_name": target.edge, "intf_name": target.intf_name}
        )
    ]


def _params(target):
    return {
        "host_name": target.edge,
        "intf_name": target.intf_name,
        "src_host": target.src_host,
        "dst_host": target.dst_host,
    }


def linux_queue_state(output: str, classid: str) -> dict[str, int]:
    """Decode tc JSON, whose HTB rates use bytes per second."""
    row = next(row for row in json.loads(output) if row["handle"] == classid)
    return {
        "rate_kbps": int(row["rate"]) * 8 // 1000,
        "ceil_kbps": int(row["ceil"]) * 8 // 1000,
        **{key: int(row["stats"][key]) for key in ("bytes", "packets", "drops")},
    }


class OverlayQosEfUndersized(ProblemBase):
    """A voice-only bandwidth template undersizes the overlay EF class.

    The operator lowers both guarantee and ceiling to one tenth of the EF
    reservation, preventing borrowing. DSCP EF and its classifier stay intact.
    HTB and RouterOS queue trees shape and tail-drop excess video. Cisco LLQ
    uses a priority policer.

    Vendor troubleshooting documents:
    [Cisco interface packet drops](https://www.cisco.com/c/en/us/support/docs/routers/asr-1000-series-ios-xe-sd-wan/224129-troubleshoot-interface-packet-drops-in.html)
    attributes output drops to QoS classes through policy counters.
    [Cisco priority queue drops](https://www.cisco.com/c/en/us/support/docs/asynchronous-transfer-mode-atm/atm-traffic-management/10105-priorityqueuedrops.html)
    describes insufficient priority bandwidth and burst allowances.
    Operator discussion:
    [Cisco Community voice bandwidth-exceed drops](https://community.cisco.com/t5/routing/b-w-exceed-drops-for-voice-traffic/td-p/2156289)
    reports EF/RTP allocation and bandwidth-exceed counters. It does not
    establish a resolved incident caused by copying a template.
    """

    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    root_cause_name = "overlay_qos_ef_undersized"
    description = (
        "Overlay egress EF class bandwidth is below its realtime traffic demand."
    )
    root_cause_owner = "node_or_k8s"
    symptom_probe = "custom"
    symptom_class = "degradation"
    symptom_desc = (
        "The br1 video call loses packets and gains jitter while pings remain healthy."
    )
    TAGS = ["vpn"]
    COMPATIBLE_COLUMNS = frozenset({"enterprise_branch"})
    supported_backends = ("kathara",)
    native_router_operations = frozenset({"set_queue_bandwidth", "queue_state"})
    Params = OverlayQosEfUndersizedParams
    BENCHMARK_TARGETS = "canonical"
    BENCHMARK_TUNNEL_IFACE = True
    SYMPTOM_WORKLOAD = REALTIME_BULK_CONTENTION

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        # XRd enterprise deployments use a different underlay; only the
        # declared FRR and RouterOS deployment profiles are validated here.
        return device_profile in (
            None,
            "",
            "frr",
            "routeros",
        ) and super().is_compatible(
            target, device_profile=device_profile, backend=backend
        )

    @classmethod
    def runs_on_device_profile(cls, device_profile):
        # Enterprise overlay queue trees exist on FRR and RouterOS edges only.
        if device_profile not in {"frr", "routeros"}:
            return False
        return super().runs_on_device_profile(device_profile)

    @classmethod
    def symptom_workload_target(cls, inject: Mapping[str, str]) -> tuple[str, ...]:
        return (inject.get("host_name") or "", inject.get("intf_name") or "")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return _params(ctx.rng.choice(_targets(cls, ctx.net_env)))

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        if ctx.scenario != "enterprise_branch" or not any(
            all(inject.get(key) == value for key, value in _params(target).items())
            for target in _targets(cls, ctx.net_env)
        ):
            raise ValueError(
                "EF bandwidth fault needs a workload-loaded overlay video egress"
            )

    def root_cause_resources(self, params):
        return [node_resource(params.host_name)]

    def qos(self, params):
        cap = platform_rate_limit_kbps(
            native_router_profile(self.net_env, params.host_name)
        )
        return overlay_qos_for(self.net_env.topo_size or "s", cap)

    def queue_state(self, params, queue="ef"):
        ops = native_fault_ops(self, params.host_name, "queue_state")
        if ops is not None:
            return ops.queue_state(f"{params.intf_name}-{queue}")
        return linux_queue_state(
            self.runtime.exec(
                params.host_name, f"tc -j -s class show dev {params.intf_name}"
            ),
            "1:10" if queue == "ef" else "1:20",
        )

    def _set_bandwidth(self, params, rate, ceil):
        ops = native_fault_ops(self, params.host_name, "set_queue_bandwidth")
        if ops is not None:
            ops.set_queue_bandwidth(f"{params.intf_name}-ef", rate, ceil)
            return
        # Preserve the healthy token bucket and the existing classifier/leaf queue.
        bucket = self.qos(params).rate_kbit * 1250 // 1000
        self.runtime.exec(
            params.host_name,
            f"tc class change dev {params.intf_name} parent 1:1 classid 1:10 "
            f"htb rate {tc_rate(rate)} ceil {tc_rate(ceil)} "
            f"burst {bucket} cburst {bucket} prio 1",
        )

    def inject_fault(self, params):
        rate = max(1, self.qos(params).ef_kbit // 10)
        self._set_bandwidth(params, rate, rate)

    def verify_fault(self, params):
        state = self.queue_state(params)
        expected = max(1, self.qos(params).ef_kbit // 10)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=state["rate_kbps"] == state["ceil_kbps"] == expected,
            details=state,
        )

    def recover_fault(self, params):
        from nika.validation.symptom.custom import _overlay_ef_baseline

        qos = self.qos(params)
        self._set_bandwidth(params, qos.ef_kbit, qos.rate_kbit)
        state = self.queue_state(params)
        healthy, traffic = _overlay_ef_baseline(self, params)
        return {
            "verified": healthy
            and state["rate_kbps"] == qos.ef_kbit
            and state["ceil_kbps"] == qos.rate_kbit,
            "details": {"queue": state, "traffic": traffic},
        }

    def oracle_playbook(self, params):
        iface, host = params.intf_name, params.host_name
        rate = max(1, self.qos(params).ef_kbit // 10)

        def drops_grow(output):
            import re

            readings = re.findall(r"(?:dropped |dropped=)(\d+)", output)
            return len(readings) >= 2 and int(readings[-1]) > int(readings[0])

        if native_router_profile(self.net_env, host) == "routeros":
            from nika.net_env.utils.routeros.parse import routeros_terse_rows

            def low_rate(output):
                return any(
                    row.get("name") == f"{iface}-ef"
                    and row.get("limit-at")
                    == row.get("max-limit")
                    == routeros_rate(rate)
                    for row in routeros_terse_rows(output)
                )
        else:

            def low_rate(output):
                config = output.splitlines()[0]
                state = linux_queue_state(config, "1:10")
                return state["rate_kbps"] == state["ceil_kbps"] == rate

        return OraclePlaybook(
            finding=f"{host}:{iface} retains EF classification but caps EF below the video demand, so its queue drops media.",
            steps=[
                shell(
                    params.dst_host,
                    "ip -4 -br addr show dev eth0; pgrep -af softphone; tail -n 6 /var/log/softphone.log",
                    "Read the video receiver's call and receive statistics.",
                    contains(r"loss=", "the receiver logs media quality"),
                ),
                router_cli(
                    self.net_env,
                    host,
                    "Confirm EF classification and growing drops in the EF queue.",
                    Check(
                        "EF retains classification, its guarantee and ceiling are undersized, and its drops grow",
                        lambda o: (
                            low_rate(o)
                            and drops_grow(o)
                            and (
                                ("00b80000/00fc0000" in o and "flowid 1:10" in o)
                                or any(
                                    "dscp=46" in line
                                    and f"new-packet-mark={iface}-ef" in line
                                    for line in o.splitlines()
                                )
                            )
                        ),
                    ),
                    frr=f"tc -j -s class show dev {iface}; tc filter show dev {iface}; tc -s class show dev {iface} classid 1:10; sleep 5; tc -s class show dev {iface} classid 1:10",
                    routeros_command=f'/queue tree print terse where name="{iface}-ef"; /ip firewall mangle print terse where out-interface="{iface}"; /queue tree print stats terse where name="{iface}-ef"; :delay 5s; /queue tree print stats terse where name="{iface}-ef"',
                    evidence=True,
                ),
            ],
        )
