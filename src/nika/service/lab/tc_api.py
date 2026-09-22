"""Shared traffic-control API for Kathara and Containerlab labs."""

from __future__ import annotations

from nika.service.lab.protocols import SupportsExec
from nika.utils.network_change_log import log_network_change


class TCMixin:
    """Linux ``tc`` operations via ``exec_cmd``."""

    def tc_set_netem(
        self: SupportsExec,
        host_name: str,
        intf_name: str,
        *,
        loss: int | None = None,
        delay_ms: int | None = None,
        jitter_ms: int | None = None,
        duplicate: int | None = None,
        corrupt: int | None = None,
        reorder: int | None = None,
        limit: int | None = None,
        handle: str | None = None,
        parent: str | None = None,
    ) -> str:
        command = f"tc qdisc add dev {intf_name}"
        if parent is not None:
            command += f" parent {parent}"
        else:
            command += " root"
        if handle is not None:
            handle = handle if handle.endswith(":") else handle + ":"
            command += f" handle {handle}"
        command += " netem"
        if loss is not None:
            command += f" loss {loss}%"
        if delay_ms is not None and jitter_ms is None:
            command += f" delay {delay_ms}ms"
        elif delay_ms is not None and jitter_ms is not None:
            command += f" delay {delay_ms}ms {jitter_ms}ms"
        if duplicate is not None:
            command += f" duplicate {duplicate}%"
        if reorder is not None:
            command += f" reorder {reorder}%"
        if corrupt is not None:
            command += f" corrupt {corrupt}%"
        if limit is not None:
            command += f" limit {limit}"
        result = self.exec_cmd(host_name, command)
        params = {
            key: value
            for key, value in {
                "loss": loss,
                "delay_ms": delay_ms,
                "jitter_ms": jitter_ms,
                "duplicate": duplicate,
                "corrupt": corrupt,
                "reorder": reorder,
                "limit": limit,
            }.items()
            if value is not None
        }
        param_text = " ".join(f"{k}={v}" for k, v in params.items())
        log_network_change(
            f"tc netem on {host_name}:{intf_name}"
            + (f" {param_text}" if param_text else ""),
            mechanism="tc_netem",
            host=host_name,
            intf=intf_name,
            params=params or None,
        )
        return result

    def tc_set_tbf(
        self: SupportsExec,
        host_name: str,
        intf_name: str,
        *,
        rate: str,
        burst: str,
        limit: str,
        handle: str | None = None,
        parent: str | None = None,
    ) -> str:
        command = f"tc qdisc add dev {intf_name}"
        if parent is not None:
            command += f" parent {parent}"
        else:
            command += " root"
        if handle is not None:
            handle = handle if handle.endswith(":") else handle + ":"
            command += f" handle {handle}"
        command += f" tbf rate {rate} burst {burst} limit {limit}"
        result = self.exec_cmd(host_name, command)
        log_network_change(
            f"tc tbf on {host_name}:{intf_name} "
            f"rate={rate} burst={burst} limit={limit}",
            mechanism="tc_tbf",
            host=host_name,
            intf=intf_name,
            params={"rate": rate, "burst": burst, "limit": limit},
        )
        return result

    def tc_clear_intf(self: SupportsExec, host_name: str, intf_name: str) -> str:
        result = self.exec_cmd(host_name, f"tc qdisc del dev {intf_name} root")
        log_network_change(
            f"tc clear on {host_name}:{intf_name}",
            mechanism="tc_clear",
            host=host_name,
            intf=intf_name,
            action="clear",
        )
        return result

    def tc_show_intf(self: SupportsExec, host_name: str, intf_name: str) -> str:
        return self.exec_cmd(host_name, f"tc qdisc show dev {intf_name}")

    def tc_show_statistics(self: SupportsExec, host_name: str, intf_name: str) -> str:
        return self.exec_cmd(host_name, f"tc -s qdisc show dev {intf_name}")

    def tc_qdisc_contains(
        self: SupportsExec, host_name: str, intf: str, keyword: str
    ) -> bool:
        output = self.tc_show_intf(host_name, intf)
        return keyword.lower() in output.lower()
