"""Traffic generation (OD-matrix iperf3, web load)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import typer

from traffic.od_flows import ODFLowGenerator
from traffic.web_access import WebBrowsingTrafficGenerator
from nika.net_env.net_env_pool import get_net_env_instance, scenario_requires_topo_size
from nika.runtime.factory import runtime_for_net_env
from nika.utils.session_resolve import resolve_running_session_id
from nika.utils.session_store import SessionStore

traffic_app = typer.Typer(help="Generate traffic in the Kathará lab.")

_TRAFFIC_TYPE_HELP: dict[str, str] = {
    "burst": "Synchronized deterministic UDP or TCP incast.",
    "od": "OD-matrix iperf3 between hosts (--od-json, --mesh-mbps, or --all-to-host + --mbps).",
    "web": "Synthetic web browsing (ab) for scenarios with web_urls.",
    "sndlib": "Replay SNDlib demands/dynamic series on isp stub hosts (interval order).",
}


def _net_env_kwargs_for_scenario(scenario: str, size: str | None) -> dict[str, Any]:
    if scenario_requires_topo_size(scenario):
        if not size:
            raise typer.BadParameter(
                f"Scenario '{scenario}' requires -s/--size (s, m, or l)."
            )
        return {"topo_size": size}
    if size is not None:
        raise typer.BadParameter(
            f"Scenario '{scenario}' does not use topology sizes; omit -s/--size."
        )
    return {}


def _resolve_lab_and_size(
    lab: str | None,
    size: str | None,
) -> tuple[str, str | None]:
    store = SessionStore()
    if lab:
        try:
            matches = [
                row
                for row in store.list_running_sessions()
                if row.get("lab_name") == lab
            ]
            if len(matches) == 1:
                meta = store.get_session(str(matches[0]["session_id"]))
                scenario = meta.get("scenario_name")
                if scenario:
                    return str(scenario), size or meta.get("scenario_topo_size")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
    try:
        resolved_id = resolve_running_session_id()
        meta = store.get_session(resolved_id)
    except (
        FileNotFoundError,
        ValueError,
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        if not lab:
            raise typer.BadParameter(
                "No valid running session found. Run `nika env run <scenario>` first, or pass --lab."
            ) from None
        return lab, size

    resolved_lab = lab or meta.get("scenario_name")
    resolved_size = size if size is not None else meta.get("scenario_topo_size")
    if not resolved_lab:
        raise typer.BadParameter(
            "Session has no scenario_name; run `nika env run` or pass --lab."
        )
    return resolved_lab, resolved_size


def _normalize_size(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    if raw not in ("s", "m", "l"):
        raise typer.BadParameter("Topology size must be one of: s, m, l.")
    return raw


def _active_runtime_lab_name(scenario: str) -> str | None:
    """Return the concrete lab name when the current session matches a scenario."""
    try:
        session_id = resolve_running_session_id()
        meta = SessionStore().get_session(session_id)
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
        return None
    if meta.get("scenario_name") != scenario:
        return None
    return str(meta.get("lab_name") or "") or None


@traffic_app.command("fetch")
def traffic_fetch(
    source: str = typer.Argument(
        ..., metavar="SOURCE", help="sndlib — download/normalize dynamic traffic."
    ),
    topo: str = typer.Option(
        ..., "--topo", help="SNDlib topology name (e.g. abilene, geant)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing normalized cache."
    ),
) -> None:
    """Fetch dynamic traffic into ``.nika_cache/sndlib/traffic/<topo>/``."""
    from nika.net_env.isp.traffic import fetch_dynamic_traffic

    src = source.strip().lower()
    if src != "sndlib":
        raise typer.BadParameter("Only SOURCE=sndlib is supported for fetch.")
    try:
        path = fetch_dynamic_traffic(topo, force=force)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"traffic_cache={path}")


@traffic_app.command("list")
def traffic_list() -> None:
    """List supported traffic types for `nika traffic run`."""
    for name, desc in sorted(_TRAFFIC_TYPE_HELP.items()):
        typer.echo(f"{name:8}  {desc}")


@traffic_app.command("run")
def traffic_run(
    traffic_type: str = typer.Argument(
        ..., metavar="TYPE", help="od | web | sndlib | burst"
    ),
    background: bool = typer.Option(
        False,
        "--background/--no-background",
        help="Run traffic in the background where supported (od/sndlib); web always blocks the CLI.",
    ),
    lab: str | None = typer.Option(
        None, "--lab", help="Kathará lab name (defaults to current session scenario)."
    ),
    size: str | None = typer.Option(
        None,
        "-s",
        "--size",
        help="Topology size s, m, or l (when the scenario uses sizes).",
    ),
    # OD (iperf3) shared
    interval: int = typer.Option(
        5, "--interval", help="iperf3 duration per client run (seconds)."
    ),
    unit: str = typer.Option(
        "M", "--unit", help='OD matrix bitrate unit suffix: "K" or "M" (iperf -b).'
    ),
    udp: bool = typer.Option(
        True, "--udp/--no-udp", help="Use UDP for iperf3 OD flows."
    ),
    server_args: str = typer.Option(
        "", "--server-args", help="Extra iperf3 server arguments."
    ),
    client_args: str = typer.Option(
        "", "--client-args", help="Extra iperf3 client arguments."
    ),
    od_json: Path | None = typer.Option(
        None, "--od-json", help="Path to JSON OD matrix: {src: {dst: rate, ...}, ...}."
    ),
    mesh_mbps: int | None = typer.Option(
        None,
        "--mesh-mbps",
        help="Start full mesh among hosts at this many Mbit/s each.",
    ),
    all_to_host: str | None = typer.Option(
        None,
        "--all-to-host",
        help="Every other host sends to this host at --mbps (Mbit/s).",
    ),
    mbps: int | None = typer.Option(
        None, "--mbps", help="Bitrate in Mbit/s for --all-to-host (od mode)."
    ),
    max_intervals: int | None = typer.Option(
        None,
        "--max-intervals",
        help="[sndlib] Replay at most N intervals (smoke / CI).",
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="[sndlib] Traffic matrix: demands (static XML) or dynamic (cache).",
    ),
    scale: float | None = typer.Option(
        None,
        "--scale",
        help="[sndlib] Multiply SNDlib rates before iperf -b (default: 1.0).",
    ),
    # web-only
    request_delay_min: float = typer.Option(
        1.0, "--request-delay-min", help="[web] Min pause between page fetches."
    ),
    request_delay_max: float = typer.Option(
        5.0, "--request-delay-max", help="[web] Max pause between page fetches."
    ),
    pages_min: int = typer.Option(
        3, "--pages-min", help="[web] Min pages per browsing session."
    ),
    pages_max: int = typer.Option(
        10, "--pages-max", help="[web] Max pages per browsing session."
    ),
    no_loop: bool = typer.Option(
        False, "--no-loop", help="[web] Run one browsing session per host then stop."
    ),
    sources: str | None = typer.Option(
        None, "--sources", help="[burst] Comma-separated source node names."
    ),
    destination: str | None = typer.Option(
        None, "--destination", help="[burst] Destination node name or address."
    ),
    protocol: str = typer.Option("udp", "--protocol", help="[burst] udp or tcp."),
    rate: str = typer.Option("10M", "--rate", help="[burst] Per-source iperf3 rate."),
    packet_size: int = typer.Option(
        1200, "--packet-size", help="[burst] Packet or write size in bytes."
    ),
    duration: int = typer.Option(
        10, "--duration", help="[burst] Flow duration in seconds."
    ),
    synchronized_start: float = typer.Option(
        0.0,
        "--synchronized-start",
        help="[burst] Unix start timestamp; 0 chooses the next second.",
    ),
    seed: int = typer.Option(42, "--seed", help="[burst] Deterministic flow seed."),
) -> None:
    """Start traffic of the given TYPE against the current lab (or ``--lab``)."""
    t = traffic_type.strip().lower()
    if t not in _TRAFFIC_TYPE_HELP:
        raise typer.BadParameter(
            f"Unknown TYPE {traffic_type!r}; try `nika traffic list`."
        )

    size_n = _normalize_size(size)
    scenario, size_resolved = _resolve_lab_and_size(lab=lab, size=size_n)

    if t == "burst":
        from traffic.burst import BurstTrafficGenerator

        if not sources or not destination:
            raise typer.BadParameter("burst requires --sources and --destination.")
        protocol_n = protocol.strip().lower()
        if protocol_n not in {"udp", "tcp"}:
            raise typer.BadParameter("--protocol must be udp or tcp.")
        if packet_size <= 0 or duration <= 0:
            raise typer.BadParameter("--packet-size and --duration must be positive.")
        kwargs = _net_env_kwargs_for_scenario(scenario, size_resolved)
        runtime_lab_name = lab or _active_runtime_lab_name(scenario)
        if runtime_lab_name:
            kwargs["lab_name"] = runtime_lab_name
        net_env = get_net_env_instance(scenario, **kwargs)
        generator = BurstTrafficGenerator(runtime_for_net_env(net_env))
        event = generator.run(
            sources=[item.strip() for item in sources.split(",") if item.strip()],
            destination=destination,
            protocol=protocol_n,  # type: ignore[arg-type]
            rate=rate,
            packet_size=packet_size,
            duration=duration,
            synchronized_start=synchronized_start,
            seed=seed,
        )
        typer.echo(json.dumps(event, indent=2))
        return

    if unit not in ("K", "M"):
        raise typer.BadParameter('--unit must be "K" or "M".')
    unit_lit: Literal["K", "M"] = unit  # type: ignore[assignment]

    if t == "sndlib":
        _run_sndlib(
            scenario=scenario,
            size_resolved=size_resolved,
            unit=unit_lit,
            udp=udp,
            background=background,
            max_intervals=max_intervals,
            mode=mode,
            scale=scale,
            server_args=server_args,
            client_args=client_args,
        )
        return

    if t == "web":
        if background:
            raise typer.BadParameter(
                "`web` traffic always blocks this CLI until interrupted; do not pass `--background`."
            )
        kwargs = _net_env_kwargs_for_scenario(scenario, size_resolved)
        runtime_lab_name = lab or _active_runtime_lab_name(scenario)
        if runtime_lab_name:
            kwargs["lab_name"] = runtime_lab_name
        gen = WebBrowsingTrafficGenerator(
            scenario_name=scenario,
            request_delay_range=(request_delay_min, request_delay_max),
            pages_per_session_range=(pages_min, pages_max),
            loop_forever=not no_loop,
            **kwargs,
        )
        asyncio.run(gen.generate_traffic())
        return

    if t == "od":
        kwargs = _net_env_kwargs_for_scenario(scenario, size_resolved)
        runtime_lab_name = lab or _active_runtime_lab_name(scenario)
        if runtime_lab_name:
            kwargs["lab_name"] = runtime_lab_name
        net_env = get_net_env_instance(scenario, **kwargs)
        hosts = list(net_env.hosts)

        od_dict: dict[str, dict[str, int]]

        if od_json is not None:
            raw = json.loads(od_json.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise typer.BadParameter(
                    "OD JSON must be an object mapping src -> {dst: int, ...}."
                )
            od_dict = {}
            for sk, dv in raw.items():
                if not isinstance(dv, dict):
                    raise typer.BadParameter(f"Invalid OD row for {sk!r}.")
                od_dict[str(sk)] = {str(dk): int(val) for dk, val in dv.items()}
            if mesh_mbps is not None or all_to_host is not None or mbps is not None:
                raise typer.BadParameter(
                    "Do not combine --od-json with --mesh-mbps / --all-to-host / --mbps."
                )
        elif mesh_mbps is not None:
            od_dict = {}
            for a in hosts:
                od_dict.setdefault(a, {})
                for b in hosts:
                    if a != b:
                        od_dict[a][b] = mesh_mbps
            if mbps is not None and all_to_host is None:
                raise typer.BadParameter(
                    "--mbps is only used with --all-to-host, not with --mesh-mbps."
                )
        elif all_to_host is not None:
            if mbps is None:
                raise typer.BadParameter("--all-to-host requires --mbps.")
            od_dict = {}
            for h in hosts:
                if h != all_to_host:
                    od_dict.setdefault(h, {})[all_to_host] = mbps
        else:
            raise typer.BadParameter(
                "od mode requires one of: --od-json, --mesh-mbps, or --all-to-host with --mbps."
            )

        gen = ODFLowGenerator(runtime=runtime_for_net_env(net_env))

        if background:
            labels = gen.start_traffic_background(
                od_dicts=od_dict,
                interval=interval,
                unit=unit_lit,
                udp=udp,
                server_args=server_args,
                client_args=client_args,
            )
            typer.echo(json.dumps({"started": labels}, indent=2))
            return

        async def _run() -> list[Any]:
            return await gen.astart_generate_traffic(
                od_dicts=od_dict,
                interval=interval,
                unit=unit_lit,
                udp=udp,
                server_args=server_args,
                client_args=client_args,
            )

        results = asyncio.run(_run())
        typer.echo(json.dumps(results, indent=2))
        return


def _run_sndlib(
    *,
    scenario: str,
    size_resolved: str | None,
    unit: Literal["K", "M"],
    udp: bool,
    background: bool,
    max_intervals: int | None,
    mode: str | None,
    scale: float | None,
    server_args: str,
    client_args: str,
) -> None:
    from traffic.sndlib_replay import SndlibTrafficReplayer
    from nika.net_env.isp.traffic import resolve_traffic_series
    from nika.net_env.isp.traffic.models import DEFAULT_TRAFFIC_SCALE
    from nika.net_env.isp.identity import (
        is_isp_scenario,
        isp_topo_from_scenario,
    )

    if not is_isp_scenario(scenario):
        raise typer.BadParameter(
            "sndlib traffic replay requires an ISP scenario (e.g. isp_abilene)."
        )

    traffic_mode = (mode or "demands").strip().lower()
    if traffic_mode not in ("demands", "dynamic"):
        raise typer.BadParameter(f"--mode must be demands or dynamic, got {mode!r}.")
    traffic_scale = DEFAULT_TRAFFIC_SCALE if scale is None else float(scale)
    if traffic_scale <= 0:
        raise typer.BadParameter("--scale must be > 0.")

    resolved_id = resolve_running_session_id()
    meta = SessionStore().get_session(resolved_id)
    params = dict(meta.get("scenario_params") or {})
    lab_name = meta.get("lab_name") or params.get("lab_name")
    kwargs = {
        k: params[k]
        for k in (
            "topo",
            "igp",
            "metric_strategy",
            "constant_metric",
            "bgp_mode",
            "device_profile",
        )
        if k in params and params[k] is not None
    }
    backend = meta.get("backend") or params.get("backend")
    if backend:
        kwargs["backend"] = backend
    if lab_name:
        kwargs["lab_name"] = lab_name
    topology_file = meta.get("topology_file") or params.get("topology_file")
    runtime_workdir = meta.get("runtime_workdir") or params.get("runtime_workdir")
    if topology_file:
        kwargs["topology_file"] = topology_file
    if runtime_workdir:
        kwargs["runtime_workdir"] = runtime_workdir
    if size_resolved is not None:
        kwargs.update(_net_env_kwargs_for_scenario(scenario, size_resolved))

    net_env = get_net_env_instance(scenario, **kwargs)
    topo = (
        params.get("topo")
        or getattr(net_env, "topo", None)
        or isp_topo_from_scenario(scenario)
    )
    series = resolve_traffic_series(topo, traffic_mode)
    if series is None:
        raise typer.BadParameter("Could not resolve SNDlib traffic series.")
    inventory = getattr(net_env, "inventory", {}) or {}
    if not (inventory.get("hosts") or inventory.get("traffic", {}).get("stubs")):
        raise typer.BadParameter(
            "isp lab has no traffic stub hosts; redeploy with a current nika build."
        )

    replayer = SndlibTrafficReplayer(runtime=runtime_for_net_env(net_env))
    results = replayer.replay(
        series,
        scale=traffic_scale,
        inventory=inventory,
        unit=unit,
        udp=udp,
        background=background,
        max_intervals=max_intervals,
        server_args=server_args,
        client_args=client_args,
    )
    typer.echo(json.dumps(results, indent=2))
