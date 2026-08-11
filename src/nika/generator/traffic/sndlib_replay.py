"""Sequential SNDlib traffic matrix replay via ODFLowGenerator."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from nika.generator.traffic.od_flows import ODFLowGenerator
from nika.net_env.isp.traffic.models import TrafficMatrixSeries
from nika.net_env.isp.traffic.od import series_to_od_dicts
from nika.runtime.base import LabRuntime


def host_ips_from_isp_inventory(inventory: dict[str, Any] | None) -> dict[str, str]:
    """Map stub host name → bare IPv4 from ISP traffic inventory (data plane)."""
    mapping: dict[str, str] = {}
    if not inventory:
        return mapping
    for row in inventory.get("hosts") or []:
        name = row.get("host")
        raw = row.get("address") or ""
        if not name or not raw:
            continue
        mapping[str(name)] = str(raw).split("/", 1)[0]
    return mapping


class SndlibTrafficReplayer:
    """Replay TrafficMatrixSeries intervals in order using iperf3 OD flows."""

    def __init__(self, runtime: LabRuntime):
        self.runtime = runtime
        self._od = ODFLowGenerator(runtime)

    def replay(
        self,
        series: TrafficMatrixSeries,
        *,
        scale: float = 1.0,
        inventory: dict[str, Any] | None = None,
        unit: Literal["K", "M"] = "K",
        udp: bool = True,
        background: bool = False,
        max_intervals: int | None = None,
        server_args: str = "",
        client_args: str = "",
    ) -> list[dict[str, Any]]:
        od_list = series_to_od_dicts(series, scale=scale, inventory=inventory)
        if max_intervals is not None:
            od_list = od_list[: max(0, int(max_intervals))]
        host_ips = host_ips_from_isp_inventory(inventory)
        results: list[dict[str, Any]] = []
        for index, (interval, od) in enumerate(
            zip(series.intervals, od_list, strict=False)
        ):
            if not od:
                results.append(
                    {
                        "index": index,
                        "skipped": True,
                        "reason": "empty_od",
                        "duration_sec": interval.duration_sec,
                    }
                )
                continue
            duration = max(1, int(interval.duration_sec))
            if background:
                labels = self._od.start_traffic_background(
                    od,
                    interval=duration,
                    unit=unit,
                    udp=udp,
                    server_args=server_args,
                    client_args=client_args,
                    host_ips=host_ips or None,
                )
                results.append(
                    {
                        "index": index,
                        "background": True,
                        "labels": labels,
                        "duration_sec": duration,
                        "flow_pairs": sum(len(v) for v in od.values()),
                        "dst_ips": {
                            dst: host_ips.get(dst)
                            for dests in od.values()
                            for dst in dests
                        },
                    }
                )
                time.sleep(duration)
            else:
                summaries = asyncio.run(
                    self._od.astart_generate_traffic(
                        od,
                        interval=duration,
                        unit=unit,
                        udp=udp,
                        server_args=server_args,
                        client_args=client_args,
                        host_ips=host_ips or None,
                    )
                )
                results.append(
                    {
                        "index": index,
                        "background": False,
                        "summaries": summaries,
                        "duration_sec": duration,
                        "flow_pairs": sum(len(v) for v in od.values()),
                    }
                )
        return results
