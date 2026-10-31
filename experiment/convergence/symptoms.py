"""Inject one failure per run and record the symptoms an agent would observe.

Each run deploys (native) or restores (Firecracker) the variant, dumps the
pre-failure state with ECMP flow probes, measures a host pingmesh, injects the
same failure with the same parameters through NIKA's normal injection
workflow, measures the pingmesh again and closes the lab. All commands go
through the session's MCP ``exec_shell`` tool, as for an agent.

    uv run python -m experiment.convergence.symptoms --mode native \
        --scenario dc_clos -s m --problem link_packet_corruption \
        --set host_name=leaf_router_0_0 --set intf_name=eth0 \
        --set corruption_percentage=30 --runs 10 --out runtime/convergence/symptoms_dc_clos_m
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import time
import traceback
from pathlib import Path
from typing import Any

from experiment.convergence.parse import parse_json, sections
from experiment.convergence.collect import (
    _dump_session,
    _firecracker_lab,
    _native_lab,
    _text,
)

PINGS = 50
PING_INTERVAL = 0.05
_LOSS = re.compile(r"(\d+) packets transmitted, (\d+) received")


def hosts_of(dump: dict) -> dict[str, str]:
    """End hosts (nodes without a routing daemon) and their first IPv4 address."""
    out = {}
    for node, v in dump.items():
        if "raw" not in v:
            continue
        secs = sections(v["raw"])
        if "router" in secs:
            continue
        for link in parse_json(secs.get("addr", "")) or []:
            if link.get("ifname") == "lo":
                continue
            addrs = [
                a["local"]
                for a in link.get("addr_info", [])
                if a.get("family") == "inet"
            ]
            if addrs:
                out[node] = addrs[0]
                break
    return out


async def pingmesh(session_id: str, scenario: str, hosts: dict[str, str]) -> dict:
    """Loss per directed host pair: (src, dst) -> fraction of pings lost.

    A ping that prints no statistics (for example, network unreachable) counts
    as total loss. A pair missing from the output (the command timed out) maps
    to ``None``.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from agent.protocols import DIAGNOSIS
    from nika.agents.mcp_config import load_session_mcp_config

    config = load_session_mcp_config(session_id, scenario, phase=DIAGNOSIS)
    tools = {
        t.name: t for t in await MultiServerMCPClient(connections=config).get_tools()
    }
    tool = tools["exec_shell"]

    async def one(src: str) -> dict[str, float | None]:
        targets = [f"{d}={ip}" for d, ip in sorted(hosts.items()) if d != src]
        # One ping stream per source at a time: concurrent streams overload
        # small lab VMs and add loss unrelated to the failure.
        cmd = (
            "for t in " + " ".join(targets) + "; do n=${t%%=*}; ip=${t#*=}; "
            f"r=$(ping -c {PINGS} -i {PING_INTERVAL} -W 1 -q $ip 2>&1 | grep transmitted); "
            'echo "$n|$r"; done'
        )
        # A ping with total loss takes about PINGS * PING_INTERVAL + 1 s.
        timeout = 30 + len(targets) * (PINGS * PING_INTERVAL + 2)
        text = _text(
            await tool.ainvoke({"host_name": src, "command": cmd, "timeout": timeout})
        )
        loss: dict[str, float | None] = {d: None for d in hosts if d != src}
        for line in text.splitlines():
            name, sep, rest = line.partition("|")
            if not sep or name.strip() not in loss:
                continue
            m = _LOSS.search(rest)
            sent, got = (int(m.group(1)), int(m.group(2))) if m else (1, 0)
            loss[name.strip()] = round(1 - got / sent, 3) if sent else 1.0
        return loss

    rows = await asyncio.gather(*(one(s) for s in sorted(hosts)))
    return {
        f"{s}->{d}": v for s, row in zip(sorted(hosts), rows) for d, v in row.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "firecracker"], required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("-s", "--size", default=None)
    parser.add_argument("--igp", default=None)
    parser.add_argument("--bgp-mode", default=None)
    parser.add_argument("--backend", default="kathara")
    parser.add_argument("--problem", required=True)
    parser.add_argument(
        "--set", action="append", default=[], help="inject parameter key=value"
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument(
        "--settle", type=int, default=10, help="seconds between injection and pingmesh"
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    params = dict(kv.split("=", 1) for kv in args.set)

    from nika.workflows.failure.inject import inject_failure

    base = Path(args.out) / args.mode
    for index in range(args.start, args.start + args.runs):
        run_dir = base / f"run_{index:02d}"
        if (run_dir / "meta.json").exists():
            continue
        run_dir.mkdir(parents=True, exist_ok=True)
        print(f"[{args.mode}] {args.scenario} {args.problem} run {index}", flush=True)
        lab = (
            _native_lab(args, run_dir)
            if args.mode == "native"
            else _firecracker_lab(args, run_dir, index)
        )
        meta: dict[str, Any] = {
            "mode": args.mode,
            "scenario": args.scenario,
            "size": args.size,
            "problem": args.problem,
            "params": params,
            "host": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
        try:
            with lab as (sid, timings, _ready):
                nodes = asyncio.run(_dump_session(sid, args.scenario, probe=True))
                (run_dir / "dump_pre.json").write_text(
                    json.dumps({"session_id": sid, "nodes": nodes})
                )
                hosts = hosts_of(nodes)
                before = asyncio.run(pingmesh(sid, args.scenario, hosts))
                injected = time.monotonic()
                inject_failure([args.problem], session_id=sid, param_overrides=params)
                meta["inject_sec"] = round(time.monotonic() - injected, 1)
                time.sleep(args.settle)
                after = asyncio.run(pingmesh(sid, args.scenario, hosts))
                meta.update(session_id=sid, timings=timings, hosts=hosts)
                (run_dir / "pingmesh.json").write_text(
                    json.dumps({"before": before, "after": after})
                )
        except Exception:  # noqa: BLE001 - one failed run must not stop the series
            meta["error"] = traceback.format_exc()
            print(meta["error"], flush=True)
            (run_dir / "error.json").write_text(json.dumps(meta, indent=1))
            continue
        (run_dir / "error.json").unlink(missing_ok=True)  # from an earlier attempt
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=1))
        lossy = sorted(k for k, v in after.items() if v >= 0.1)
        print(f"  {len(lossy)}/{len(after)} pairs lose >=10%: {lossy[:6]}", flush=True)


if __name__ == "__main__":
    main()
