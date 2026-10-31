"""Deploy or restore one environment variant repeatedly and dump its state.

Both modes read the lab through the session's MCP ``exec_shell`` tool, the
interface agents use, so native and Firecracker runs share one instrument:

* ``native``: ``start_net_env`` deploys from scratch and verifies; the dump runs
  at the point where a benchmark trial would inject its failure.
* ``firecracker``: a ``TrialLab`` restores the variant's lab image and attaches
  (and verifies) the session, as a benchmark trial does before injection.

Each run writes ``<out>/<mode>/run_NN/dump_<t>.json`` per dump delay ``t``.

    uv run python -m experiment.convergence.collect --mode native \
        --scenario campus_lan -s s --runs 10 --out runtime/convergence/campus_lan_s
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import socket
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCRIPT = (Path(__file__).with_name("node_dump.sh")).read_text()
CHUNK = 3000  # stay under per-command output limits of every backend
UPLOAD_CHUNK = 60000  # bytes of script per upload command


def _text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in result
        )
    return str(result)


async def _dump_node(tool: Any, node: str, script: str = SCRIPT) -> dict[str, Any]:
    async def run(cmd: str, timeout: float = 60) -> str:
        out = await tool.ainvoke(
            {"host_name": node, "command": cmd, "timeout": timeout}
        )
        return _text(out)

    script_b64 = base64.b64encode(gzip.compress(script.encode())).decode()
    # Upload in pieces: one command line must stay under the kernel's limit.
    await run("rm -f /tmp/.nika_conv.b64")
    for start in range(0, len(script_b64), UPLOAD_CHUNK):
        piece = script_b64[start : start + UPLOAD_CHUNK]
        await run(f"printf %s {piece} >> /tmp/.nika_conv.b64")
    size_txt = await run(
        "base64 -d /tmp/.nika_conv.b64 | gunzip > /tmp/.nika_conv.sh && "
        "sh /tmp/.nika_conv.sh | gzip -9 | base64 | tr -d '\\n' > /tmp/.nika_conv.out && "
        "wc -c < /tmp/.nika_conv.out",
        timeout=300,
    )
    try:
        size = int(size_txt.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"dump failed: {size_txt[:500]}"}
    parts = []
    for start in range(1, size + 1, CHUNK):
        want = min(CHUNK, size - start + 1)
        for _attempt in range(4):
            part = (
                await run(f"cut -c {start}-{start + CHUNK - 1} /tmp/.nika_conv.out")
            ).strip()
            if len(part) == want:
                break
        parts.append(part)
    await run("rm -f /tmp/.nika_conv.b64 /tmp/.nika_conv.sh /tmp/.nika_conv.out")
    blob = "".join(parts)
    try:
        raw = gzip.decompress(base64.b64decode(blob)).decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 - record and keep other nodes
        return {"error": f"decode failed ({exc}); size={size} got={len(blob)}"}
    return {"raw": raw}


PROBE_SOURCES = 16


def probe_script(node: str, dump: dict[str, dict]) -> str | None:
    """``ip route get`` for sampled flows through every ECMP route of ``node``.

    With ``fib_multipath_hash_policy=0`` Linux hashes source and destination
    address, so each (source, destination) pair is one flow. Under policy 1
    (layer 4, used on ``k8s_lab`` core routers) a probe stands for the flow
    with protocol 0 and no ports. Sources are an evenly spaced sample of the
    lab's addresses outside management networks; destinations are the lowest
    such addresses inside the prefix (or its first host address).
    """
    from ipaddress import IPv4Address, IPv4Network

    from experiment.convergence.parse import is_mgmt, parse_json, sections

    secs = {n: sections(v["raw"]) for n, v in dump.items() if "raw" in v}
    if node not in secs:
        return None
    addrs, local, ifaces = set(), set(), []
    for n, sec in secs.items():
        for link in parse_json(sec.get("addr", "")) or []:
            for a in link.get("addr_info", []):
                ip = a.get("local", "")
                if (
                    a.get("family") == "inet"
                    and not ip.startswith("127.")
                    and not is_mgmt(ip)
                ):
                    addrs.add(ip)
                    if n == node:
                        local.add(ip)
                        ifaces.append(link["ifname"])
    pool = sorted(addrs - local, key=IPv4Address)
    step = max(1, len(pool) // PROBE_SOURCES)
    sources = pool[::step][:PROBE_SOURCES]
    lines = []
    for r in parse_json(secs[node].get("route4", "")) or []:
        hops = r.get("nexthops") or []
        if r.get("table", "main") != "main" or len(hops) < 2:
            continue
        dst = r.get("dst")
        try:
            net = IPv4Network("0.0.0.0/0" if dst == "default" else dst, strict=False)
        except ValueError:
            continue
        inside = sorted(
            (a for a in addrs if IPv4Address(a) in net and net.prefixlen > 0),
            key=IPv4Address,
        )[:2]
        targets = inside or (
            [str(net.network_address + 1)] if net.prefixlen else ["198.18.0.1"]
        )
        egress = {h.get("dev") for h in hops}
        iif = next(
            (i for i in ifaces if i not in egress), ifaces[0] if ifaces else "lo"
        )
        for d in targets:
            for src in sources:
                if src == d:
                    continue
                lines.append(
                    f"echo 'P|{dst}|{src}|{d}|'$(ip route get {d} from {src} iif {iif} 2>&1 | head -1)"
                )
    return "\n".join(lines) + "\n" if lines else None


async def _probe_nodes(
    tool: Any, nodes: dict[str, dict], sem: asyncio.Semaphore
) -> None:
    async def one(node: str) -> None:
        script = probe_script(node, nodes)
        if script is None:
            return
        async with sem:
            try:
                result = await _dump_node(tool, node, script)
            except Exception as exc:  # noqa: BLE001
                result = {"error": repr(exc)}
        nodes[node]["probes"] = result.get("raw") or f"error: {result.get('error')}"

    await asyncio.gather(*(one(n) for n in list(nodes)))


async def _dump_session(session_id: str, scenario: str, probe: bool = False) -> dict:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from agent.protocols import DIAGNOSIS
    from nika.agents.mcp_config import load_session_mcp_config
    from nika.workflows.session.containers import list_session_containers

    _sid, _lab, rows = list_session_containers(session_id)
    nodes = sorted({str(r.get("name") or "").strip() for r in rows} - {""})
    # The session records its backend (ISP scenarios support two).
    config = load_session_mcp_config(session_id, scenario, phase=DIAGNOSIS)
    client = MultiServerMCPClient(connections=config)
    tools = {tool.name: tool for tool in await client.get_tools()}
    tool = tools["exec_shell"]
    sem = asyncio.Semaphore(8)

    async def one(node: str) -> tuple[str, dict]:
        async with sem:
            try:
                return node, await _dump_node(tool, node)
            except Exception as exc:  # noqa: BLE001
                return node, {"error": repr(exc)}

    results = dict(await asyncio.gather(*(one(n) for n in nodes)))
    if probe:
        await _probe_nodes(tool, results, sem)
    return results


def _dump_series(session_id, scenario, delays, run_dir, t_ready) -> None:
    for delay in delays:
        wait = t_ready + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        started = time.monotonic()
        last = delay == delays[-1]
        nodes = asyncio.run(_dump_session(session_id, scenario, probe=last))
        doc = {
            "session_id": session_id,
            "delay_sec": delay,
            "dump_started_after_ready_sec": round(started - t_ready, 1),
            "dump_duration_sec": round(time.monotonic() - started, 1),
            "nodes": nodes,
        }
        (run_dir / f"dump_{delay}.json").write_text(json.dumps(doc))
        errors = [n for n, v in nodes.items() if "error" in v]
        print(
            f"  dump t+{delay}s: {len(nodes)} nodes, {len(errors)} errors"
            + (f" {errors[:5]}" if errors else ""),
            flush=True,
        )


def _isp_keys(args) -> dict[str, Any]:
    keys = {"igp": args.igp, "bgp_mode": args.bgp_mode, "backend": args.backend}
    return {k: v for k, v in keys.items() if v}


@contextmanager
def _native_lab(args, run_dir: Path):
    from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
    from nika.utils.session import Session
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session

    started = time.monotonic()
    sid = start_net_env(
        args.scenario,
        args.size,
        redeploy=True,
        result_dir=str(run_dir / "session"),
        session_tag="conv",
        **_isp_keys(args),
    )
    ready = time.monotonic()
    session = Session()
    session.load_running_session(session_id=sid)
    try:
        # Mount the servers of the session's backend (ISP scenarios support two),
        # as an agent run does.
        with mcp_gateway_for_session(
            sid, scenario_name=args.scenario, backend=getattr(session, "backend", None)
        ):
            yield sid, {"deploy_sec": round(ready - started, 1)}, ready
    finally:
        close_session(session_id=sid)


@contextmanager
def _firecracker_lab(args, run_dir: Path, index: int):
    from nika.firecracker.image import variant
    from nika.firecracker.trial import TrialLab
    from nika.remote.workflows import remote_mcp_gateway
    from nika.workflows.session.close import close_session

    key = f"conv-{args.scenario}-{args.size or 'na'}-{run_dir.parent.parent.name}-{index:02d}"
    started = time.monotonic()
    with TrialLab(
        variant(scenario=args.scenario, topo_size=args.size, **_isp_keys(args)),
        trial_key=key,
        owner=str(run_dir.parent.resolve()),
    ) as lab:
        sid = lab.attach(session_id=key, session_dir=str(run_dir / "session"))
        ready = time.monotonic()
        try:
            with remote_mcp_gateway(sid):
                yield (
                    sid,
                    {"restore_sec": round(ready - started, 1), **lab.timings},
                    ready,
                )
        finally:
            close_session(session_id=sid, undeploy=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "firecracker"], required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("-s", "--size", default=None)
    parser.add_argument("--igp", default=None, help="ISP topologies: isis or ospf")
    parser.add_argument(
        "--bgp-mode", default=None, help="ISP topologies: none, ibgp_rr, ebgp"
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="kathara or containerlab (default: scenario default)",
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--start", type=int, default=1, help="first run index")
    parser.add_argument(
        "--delays",
        default="0,60",
        help="comma-separated seconds after the lab is ready at which to dump",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    # Probes go into the last dump, which the analyses read.
    delays = sorted({int(x) for x in args.delays.split(",")})
    base = Path(args.out) / args.mode
    for index in range(args.start, args.start + args.runs):
        run_dir = base / f"run_{index:02d}"
        if (run_dir / "meta.json").exists():
            print(f"skip {run_dir} (done)")
            continue
        run_dir.mkdir(parents=True, exist_ok=True)
        print(f"[{args.mode}] {args.scenario} {args.size} run {index}", flush=True)
        lab = (
            _native_lab(args, run_dir)
            if args.mode == "native"
            else _firecracker_lab(args, run_dir, index)
        )
        meta: dict[str, Any] = {
            "mode": args.mode,
            "scenario": args.scenario,
            "size": args.size,
            "index": index,
            "started_at": time.time(),
            # Linux's ECMP hash key is per kernel boot: compare flows per host.
            "host": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
        try:
            with lab as (sid, timings, ready):
                meta.update(session_id=sid, timings=timings)
                _dump_series(sid, args.scenario, delays, run_dir, ready)
        except Exception:  # noqa: BLE001 - one failed run must not stop the series
            meta["error"] = traceback.format_exc()
            print(meta["error"], flush=True)
            (run_dir / "error.json").write_text(json.dumps(meta, indent=1))
            continue
        (run_dir / "error.json").unlink(missing_ok=True)  # from an earlier attempt
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
