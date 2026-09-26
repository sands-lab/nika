"""Sampled host-to-host ping reachability shared by lab host APIs."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

_STAT_KEYS = (
    "tx",
    "rx",
    "loss_percent",
    "time_ms",
    "rtt_avg_ms",
    "rtt_min_ms",
    "rtt_max_ms",
    "rtt_mdev_ms",
    "status",
)


class PingReachabilityMixin:
    """Sampled ping reachability for host APIs that provide ``exec_cmd_async``."""

    async def _check_ping_success_async(self, host: str, dst_ip: str) -> dict:
        """Ping ``dst_ip`` twice from ``host`` and return parsed statistics."""
        # Lazy: the pingmesh package imports the Containerlab host API.
        from nika.service.pingmesh.parser import parse_ping_output

        output = await self.exec_cmd_async(host, f"ping -c 2 -n -q {dst_ip}")
        return dict(parse_ping_output(output if isinstance(output, str) else ""))

    async def _sampled_reachability(self, host_ips: dict[str, str | None]) -> str:
        """Ping from every host to up to two sampled destinations; return JSON.

        Destinations without an address are not pinged.
        """
        host_list = sorted(host_ips.items())
        if len(host_list) > 2:
            dst_list = host_list.copy()
            random.shuffle(dst_list)
            dst_list = dst_list[:2]
        else:
            dst_list = host_list

        pairs: list[tuple[str, str]] = []
        coroutines = []
        for src_name, _ in host_list:
            for dst_name, dst_ip in dst_list:
                if src_name == dst_name or not dst_ip:
                    continue
                pairs.append((src_name, dst_name))
                coroutines.append(self._check_ping_success_async(src_name, dst_ip))
        responses = await asyncio.gather(*coroutines)

        results = []
        for (src, dst), stats in zip(pairs, responses):
            entry: dict[str, Any] = {
                "src": src,
                "dst": dst,
                "dst_ip": host_ips.get(dst),
            }
            entry.update({key: stats.get(key) for key in _STAT_KEYS})
            results.append(entry)
        return json.dumps(
            {"hosts": host_ips, "results": results}, separators=(",", ":")
        )
