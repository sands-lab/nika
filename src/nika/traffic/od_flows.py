import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from nika.runtime.base import LabRuntime
from nika.utils.parallel import bounded_parallel_map

# iperf3 servers on one destination listen on consecutive ports, one per source.
OD_START_PORT = 5201
# Per-host scratch directory for iperf3 reports and server PID files.
OD_WORKDIR = "/tmp/nika-od"
_MARKER = "@@NIKA_OD"
# iperf3 3.12 waits 30 s for a UDP setup reply lost to congestion; stop such
# clients shortly after the transfer would have ended instead.
_CLIENT_GRACE_SEC = 10
# Exec budget for iperf3 setup and teardown on top of the transfer itself.
_EXEC_MARGIN_SEC = 30


class _ExecFn(Protocol):
    def __call__(self, node: str, cmd: str, *, timeout: float = 10.0) -> str: ...


@dataclass(frozen=True)
class ODFlow:
    src: str
    dst: str
    port: int
    volume: int

    @property
    def label(self) -> str:
        return f"{self.src}_to_{self.dst}"


def plan_od_flows(od_dicts: dict[str, dict[str, int]]) -> list[ODFlow]:
    """Assign each src != dst pair its own server port on the destination."""
    next_port: dict[str, int] = {}
    flows: list[ODFlow] = []
    for src, dests in od_dicts.items():
        for dst, volume in dests.items():
            if src == dst:
                continue
            port = next_port.get(dst, OD_START_PORT)
            next_port[dst] = port + 1
            flows.append(ODFlow(src, dst, port, volume))
    return flows


def _group(flows: list[ODFlow], key: Callable[[ODFlow], str]) -> dict[str, list]:
    groups: dict[str, list[ODFlow]] = {}
    for flow in flows:
        groups.setdefault(key(flow), []).append(flow)
    return groups


def _server_start_cmd(
    ports: list[int], server_args: str, lifetime: int, run: str | None
) -> str:
    """Start one-shot servers in the background and wait until they listen.

    ``timeout`` bounds a server whose client never connects, and a server left
    on the same port by an earlier run is replaced, so servers never pile up.
    Reports go to per-run files when ``run`` is set, else they are discarded.
    """
    d = OD_WORKDIR
    plist = " ".join(str(p) for p in ports)
    report = f">{d}/{run}_s_$p.json 2>/dev/null" if run else ">/dev/null 2>&1"
    return (
        f"mkdir -p {d}; k=0; for p in {plist}; do "
        f'[ -s {d}/s_$p.pid ] && kill "$(cat {d}/s_$p.pid)" 2>/dev/null && k=1; '
        'done; [ "$k" = 1 ] && sleep 0.5; '
        f"for p in {plist}; do timeout {lifetime} iperf3 -s -1 -p $p {server_args} "
        f"-J {report} & echo $! >{d}/s_$p.pid; done; "
        "i=0; while [ $i -lt 50 ]; do l=$(ss -ltn); ok=1; "
        f'for p in {plist}; do case "$l" in *":$p "*) ;; *) ok=0;; esac; done; '
        '[ "$ok" = 1 ] && break; i=$((i+1)); sleep 0.1; done'
    )


def _server_collect_cmd(ports: list[int], run: str) -> str:
    """Wait briefly for servers to exit, print their reports, and clean up."""
    d = OD_WORKDIR
    plist = " ".join(str(p) for p in ports)
    return (
        "i=0; while [ $i -lt 50 ]; do alive=0; "
        f'for p in {plist}; do kill -0 "$(cat {d}/s_$p.pid 2>/dev/null)" '
        "2>/dev/null && alive=1; done; "
        '[ "$alive" = 0 ] && break; i=$((i+1)); sleep 0.1; done; '
        f'for p in {plist}; do kill "$(cat {d}/s_$p.pid 2>/dev/null)" 2>/dev/null; '
        f"echo {_MARKER} $p; cat {d}/{run}_s_$p.json 2>/dev/null; echo; "
        f"rm -f {d}/{run}_s_$p.json {d}/s_$p.pid; done"
    )


def _client_cmd(
    flow: ODFlow,
    dst_ip: str,
    *,
    unit: str,
    interval: int,
    udp: bool,
    client_args: str,
) -> str:
    if udp:
        client_args += " -u"
    return (
        f"timeout {interval + _CLIENT_GRACE_SEC} "
        f"iperf3 -c {dst_ip} -p {flow.port} -b {flow.volume}{unit} "
        f"-t {interval} {client_args} -l 1472 -J"
    )


def _split_marked(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in (output or "").splitlines():
        if line.startswith(_MARKER + " "):
            current = sections.setdefault(line[len(_MARKER) + 1 :].strip(), [])
        elif current is not None:
            current.append(line)
    return {key: "\n".join(lines) for key, lines in sections.items()}


def _resolve_dst_ips(
    flows: list[ODFlow],
    host_ips: dict[str, str] | None,
    resolve: Callable[[str], str | None],
) -> dict[str, str]:
    ips: dict[str, str] = {}
    for dst in dict.fromkeys(flow.dst for flow in flows):
        ip = (host_ips or {}).get(dst) or resolve(dst)
        if not ip:
            raise ValueError(f"Cannot resolve IP for host {dst!r}")
        ips[dst] = ip
    return ips


def _start_servers(
    exec_fn: _ExecFn,
    flows: list[ODFlow],
    server_args: str,
    interval: int,
    run: str | None,
) -> dict[str, list[ODFlow]]:
    by_dst = _group(flows, lambda f: f.dst)
    lifetime = interval + 2 * _EXEC_MARGIN_SEC

    def start(dst: str) -> str:
        ports = [f.port for f in by_dst[dst]]
        cmd = _server_start_cmd(ports, server_args, lifetime, run)
        return exec_fn(dst, cmd, timeout=_EXEC_MARGIN_SEC)

    bounded_parallel_map(start, list(by_dst))
    return by_dst


def start_od_flows_background(
    exec_fn: _ExecFn,
    od_dicts: dict[str, dict[str, int]],
    *,
    resolve_ip: Callable[[str], str | None],
    interval: int,
    unit: str,
    udp: bool,
    server_args: str = "",
    client_args: str = "",
    host_ips: dict[str, str] | None = None,
) -> list[str]:
    """Start OD flows without waiting: one exec per host, hosts in parallel."""
    flows = plan_od_flows(od_dicts)
    dst_ips = _resolve_dst_ips(flows, host_ips, resolve_ip)
    _start_servers(exec_fn, flows, server_args, interval, run=None)
    by_src = _group(flows, lambda f: f.src)

    def start_clients(src: str) -> str:
        cmd = " ".join(
            _client_cmd(
                f,
                dst_ips[f.dst],
                unit=unit,
                interval=interval,
                udp=udp,
                client_args=client_args,
            )
            + " >/dev/null 2>&1 &"
            for f in by_src[src]
        )
        return exec_fn(src, cmd, timeout=_EXEC_MARGIN_SEC)

    bounded_parallel_map(start_clients, list(by_src))
    return [flow.label for flow in flows]


def to_json(x):
    if isinstance(x, list):
        x = "".join(x).strip()
    if isinstance(x, str):
        x = x.strip()
        # Tolerate diagnostics printed around the report (e.g. iperf3 warnings).
        start = x.find("{")
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(x, start)[0]
            except json.JSONDecodeError:
                pass
        return {"raw": x}
    return x


class ODFLowGenerator:
    """Generate iperf3 OD-matrix traffic via backend-neutral ``LabRuntime``."""

    def __init__(self, runtime: LabRuntime):
        self.runtime = runtime

    def _extract_iperf3_summary(
        self, server_result: dict, client_result: dict, unit: str
    ) -> dict:
        for side, result in (("client", client_result), ("server", server_result)):
            if isinstance(result, dict) and result.get("error"):
                return {"error": f"iperf3 {side} error: {result['error']}"}
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
                # Only UDP reports carry jitter, loss, and packet counts.
                "jitter_ms": None if jitter_ms is None else round(jitter_ms, 6),
                "lost_percent": (
                    None if lost_percent is None else round(lost_percent, 2)
                ),
                "packets": None if packets is None else int(packets),
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
    ) -> list[dict]:
        return await asyncio.to_thread(
            self._generate_traffic,
            od_dicts,
            interval=interval,
            unit=unit,
            udp=udp,
            server_args=server_args,
            client_args=client_args,
            host_ips=host_ips,
        )

    def _generate_traffic(
        self,
        od_dicts: dict[str, dict[str, int]],
        *,
        interval: int,
        unit: Literal["K", "M"],
        udp: bool,
        server_args: str,
        client_args: str,
        host_ips: dict[str, str] | None,
    ) -> list[dict]:
        """Run every flow for ``interval`` seconds and summarize each one.

        Each host gets one exec per phase (start servers, run clients, collect
        server reports), so exec concurrency scales with hosts, not flows.
        """
        flows = plan_od_flows(od_dicts)
        if not flows:
            return []
        dst_ips = _resolve_dst_ips(flows, host_ips, self.runtime.get_data_plane_host_ip)
        # Per-run report names: a late process from another run never touches
        # this run's files.
        run = uuid.uuid4().hex[:8]
        by_dst = _start_servers(
            self.runtime.exec, flows, server_args, interval, run=run
        )
        by_src: dict[str, list[tuple[int, ODFlow]]] = {}
        for index, flow in enumerate(flows):
            by_src.setdefault(flow.src, []).append((index, flow))

        def run_clients(src: str) -> dict[str, str]:
            d = OD_WORKDIR
            parts = [f"mkdir -p {d};"]
            for index, flow in by_src[src]:
                client = _client_cmd(
                    flow,
                    dst_ips[flow.dst],
                    unit=unit,
                    interval=interval,
                    udp=udp,
                    client_args=client_args,
                )
                # stderr stays out of the report (iperf3 warns about -l 1472).
                parts.append(f"{client} >{d}/{run}_c_{index}.json 2>/dev/null &")
            parts.append("wait;")
            for index, _ in by_src[src]:
                report = f"{d}/{run}_c_{index}.json"
                parts.append(
                    f"echo {_MARKER} {index}; cat {report}; echo; rm -f {report};"
                )
            output = self.runtime.exec(
                src, " ".join(parts), timeout=interval + _EXEC_MARGIN_SEC
            )
            return _split_marked(output)

        def collect_servers(dst: str) -> dict[str, str]:
            cmd = _server_collect_cmd([flow.port for flow in by_dst[dst]], run)
            return _split_marked(self.runtime.exec(dst, cmd, timeout=_EXEC_MARGIN_SEC))

        # Every client exec must run at once: each blocks for the interval.
        client_reports: dict[str, str] = {}
        for reports in bounded_parallel_map(
            run_clients, list(by_src), max_workers=len(by_src)
        ):
            client_reports.update(reports)
        server_reports = dict(
            zip(by_dst, bounded_parallel_map(collect_servers, list(by_dst)))
        )
        return [
            self._extract_iperf3_summary(
                server_result=to_json(server_reports[flow.dst].get(str(flow.port), "")),
                client_result=to_json(client_reports.get(str(index), "")),
                unit=unit,
            )
            for index, flow in enumerate(flows)
        ]

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
