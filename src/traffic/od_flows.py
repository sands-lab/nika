import asyncio
import json
from typing import Literal

from nika.runtime.base import LabRuntime


def to_json(x):
    if isinstance(x, list):
        x = "".join(x).strip()
    if isinstance(x, str):
        x = x.strip()
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return {"raw": x}
    return x


class ODFLowGenerator:
    """Generate iperf3 OD-matrix traffic via backend-neutral ``LabRuntime``."""

    def __init__(self, runtime: LabRuntime):
        self.runtime = runtime

    async def _arun_server(self, dst_host: str, dst_port: int, server_args: str):
        cmd = f"iperf3 -s -1 -p {dst_port} {server_args} -J"
        result = await asyncio.to_thread(self.runtime.exec, dst_host, cmd)
        return to_json(result)

    def _run_server(self, dst_host: str, dst_port: int, server_args: str):
        cmd = f"iperf3 -s -1 -p {dst_port} {server_args} -J &"
        result = self.runtime.exec(dst_host, cmd)
        return to_json(result)

    async def _arun_client(
        self,
        src_host: str,
        dst_ip: str,
        dst_port: int,
        volume: int,
        unit: str,
        interval: int,
        udp: bool,
        client_args: str,
        background: bool = False,
    ):
        if udp:
            client_args += " -u"
        cmd = f"iperf3 -c {dst_ip} -p {dst_port} -b {volume}{unit} -t {interval} {client_args} -l 1472 -J"
        if background:
            cmd += " &"
        result = await asyncio.to_thread(self.runtime.exec, src_host, cmd)
        return to_json(result)

    def _run_client(
        self,
        src_host: str,
        dst_ip: str,
        dst_port: int,
        volume: int,
        unit: str,
        interval: int,
        udp: bool,
        client_args: str,
    ):
        if udp:
            client_args += " -u"
        cmd = f"iperf3 -c {dst_ip} -p {dst_port} -b {volume}{unit} -t {interval} {client_args} -l 1472 -J &"
        result = self.runtime.exec(src_host, cmd)
        return to_json(result)

    def _extract_iperf3_summary(
        self, server_result: dict, client_result: dict, unit: str
    ) -> dict:
        try:
            start = server_result["start"]
            server_end = server_result["end"]

            local_host = start["connected"][0]["local_host"]
            server_port = start["connected"][0]["local_port"]
            remote_host = start["connected"][0]["remote_host"]
            remote_port = start["connected"][0]["remote_port"]

            protocol = start["test_start"]["protocol"]
            timestamp = start["timestamp"]["time"]
            duration = start["test_start"]["duration"]
            target_bps = start["test_start"]["target_bitrate"]

            if "sum_received" in server_end:
                summary = server_end["sum_received"]
            else:
                summary = server_end["sum"]

            avg_bps = summary["bits_per_second"]
            if "sum_sent" in client_result["end"]:
                client_summary = client_result["end"]["sum_sent"]
            else:
                client_summary = client_result["end"]["sum"]
            client_avg_bps = client_summary["bits_per_second"]

            if unit == "K":
                target_bits_per_sec = target_bps / 1000
                avg_recv_bits_per_sec = avg_bps / 1000
                avg_sent_bits_per_sec = client_avg_bps / 1000

            elif unit == "M":
                target_bits_per_sec = target_bps / 1_000_000
                avg_recv_bits_per_sec = avg_bps / 1_000_000
                avg_sent_bits_per_sec = client_avg_bps / 1_000_000

            jitter_ms = summary.get("jitter_ms", None)
            lost_percent = summary.get("lost_percent", None)
            packets = summary.get("packets", None)

            server_cpu_usage = server_end["cpu_utilization_percent"]["host_total"]
            client_cpu_usage = client_result["end"]["cpu_utilization_percent"][
                "host_total"
            ]

            return {
                "client_ip": remote_host,
                "client_port": remote_port,
                "server_ip": local_host,
                "server_port": server_port,
                "protocol": protocol,
                "timestamp": timestamp,
                "duration": duration,
                f"target_{unit}bits_per_sec": round(target_bits_per_sec, 2),
                f"avg_sent_{unit}bits_per_sec": round(avg_sent_bits_per_sec, 2),
                f"avg_recv_{unit}bits_per_sec": round(avg_recv_bits_per_sec, 2),
                "jitter_ms": round(jitter_ms, 6),
                "lost_percent": round(lost_percent, 2),
                "packets": int(packets),
                "server_cpu_usage_percent": round(server_cpu_usage, 2),
                "client_cpu_usage_percent": round(client_cpu_usage, 2),
            }
        except Exception as e:
            return {
                "error": f"Failed to extract iperf3 summary: {e}.\n"
                f"server_result: {server_result}\n\n"
                f"client_result: {client_result}\n"
            }

    async def astart_generate_traffic(
        self,
        od_dicts: dict[str, dict[str, int]],
        interval: int = 5,
        unit: Literal["K", "M"] = "K",
        udp: bool = True,
        server_args: str = "",
        client_args: str = "",
        host_ips: dict[str, str] | None = None,
    ) -> list[str]:
        client_coroutines = []
        server_coroutines = []

        labels = []

        start_port_id = 5201
        started_server_ports = {}
        server_port_assign = {}
        ip_overrides = dict(host_ips or {})

        for src_host, dests in od_dicts.items():
            for dst_host, volume in dests.items():
                if src_host == dst_host:
                    continue

                labels.append(f"{src_host}_to_{dst_host}")

                if dst_host in started_server_ports.keys():
                    started_server_ports[dst_host] = started_server_ports[dst_host] + 1
                    dst_port = started_server_ports[dst_host]
                    server_port_assign[dst_host][src_host] = dst_port
                    server_task = asyncio.create_task(
                        self._arun_server(
                            dst_host=dst_host,
                            dst_port=dst_port,
                            server_args=server_args,
                        )
                    )

                else:
                    server_task = asyncio.create_task(
                        self._arun_server(
                            dst_host=dst_host,
                            dst_port=start_port_id,
                            server_args=server_args,
                        )
                    )
                    started_server_ports[dst_host] = start_port_id
                    server_port_assign[dst_host] = {src_host: start_port_id}

                server_coroutines.append(server_task)

        await asyncio.sleep(0.2)

        for src_host, dests in od_dicts.items():
            for dst_host, volume in dests.items():
                dst_ip = ip_overrides.get(
                    dst_host
                ) or self.runtime.get_data_plane_host_ip(dst_host)
                if not dst_ip:
                    raise ValueError(f"Cannot resolve IP for host {dst_host!r}")
                dst_port = server_port_assign[dst_host][src_host]

                client_coroutines.append(
                    self._arun_client(
                        src_host=src_host,
                        dst_ip=dst_ip,
                        dst_port=dst_port,
                        volume=volume,
                        unit=unit,
                        interval=interval,
                        udp=udp,
                        client_args=client_args,
                    )
                )

        client_results = await asyncio.gather(*client_coroutines)
        server_results = await asyncio.gather(*server_coroutines)
        summary_results = []
        for server_res, client_res in zip(server_results, client_results):
            summary = self._extract_iperf3_summary(
                server_result=server_res, client_result=client_res, unit=unit
            )
            summary_results.append(summary)

        return summary_results

    def start_traffic_background(
        self,
        od_dicts: dict[str, dict[str, int]],
        interval: int = 5,
        unit: Literal["K", "M"] = "K",
        udp: bool = True,
        server_args: str = "",
        client_args: str = "",
        host_ips: dict[str, str] | None = None,
    ) -> list[str]:
        return self.runtime.start_background_od_traffic(
            od_dicts,
            interval=interval,
            unit=unit,
            udp=udp,
            server_args=server_args,
            client_args=client_args,
            host_ips=host_ips,
        )
