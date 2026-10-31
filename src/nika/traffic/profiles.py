"""Background traffic profiles that a benchmark case selects with ``traffic:``.

A profile adds traffic on top of the scenario's business workload
(:mod:`nika.traffic.workload`). It is built from three independent choices,
the way network measurement and transport studies describe experiment
traffic:

- **Matrix** (``pattern``): which hosts send to which, and how much.
  ``uniform`` spreads load over every permitted pair (pFabric, HPCC, Homa).
  ``gravity`` draws exponential in/out masses per host and weights each pair
  by their product (Roughan, CCR 2005; the synthetic matrices of WAN traffic
  engineering studies). ``demand`` uses the scenario's measured demand matrix,
  such as an SNDlib matrix. ``permutation`` pairs every host with one other
  host (VL2, Hedera). ``hotspot`` sends ``hot_fraction`` of the load to
  ``hotspots`` hosts. ``incast`` is ``uniform`` plus a synchronized fan-in:
  ``fan_in`` hosts answer one receiver's query at the same moment (DCTCP).
- **Flow sizes** (``flow_sizes``): an empirical CDF, clipped at
  ``max_flow_kb`` because lab links carry a few Mbit/s. See
  :mod:`nika.traffic.apps.flowgen`.
- **Arrivals**: open-loop Poisson at ``load_mbps`` of offered load summed over
  the lab, optionally modulated by a sine of ``diurnal_period_sec``.

When the routers' platform caps each interface
(``NetworkEnvBase.router_rate_limit_kbps``), or the scenario shapes its paths
below that (``NetworkEnvBase.path_rate_limit_kbps``), the profile's load,
each flow's rate, and incast bursts shrink to ``PROFILE_RATE_SHARE`` of it.

Endpoints are the nodes PingMesh probes (hosts, reachability targets, and
service nodes) that have a data-plane IPv4 address and a Python runtime. Routers carry the traffic whatever their platform (FRR, IOS
XR, RouterOS, SR Linux). Pairs the scenario isolates by design
(:class:`nika.net_env.probe_intent.ProbeIntent`) carry none.
"""

from __future__ import annotations

import ipaddress
import math
import random
import re
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nika.traffic.workload import (
    PROFILE_RATE_SHARE,
    BusinessWorkload,
    WorkloadProcess,
    rate_budget_kbps,
)
from nika.utils.parallel import bounded_parallel_map

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

Pattern = Literal["uniform", "gravity", "demand", "permutation", "hotspot", "incast"]
PATTERNS: tuple[str, ...] = Pattern.__args__
FLOW_SIZES: tuple[str, ...] = ("websearch", "datamining", "hadoop")

FLOW_PORT = 7001
INCAST_PORT = 7002
INCAST_DATAGRAM = 1400
# Installed command names; ``paused_workload`` also holds these still.
SENDER_COMMAND = "flowgen"
SINK_COMMAND = "flowsink"
INCAST_WORKER_COMMAND = "fanin-shard"
INCAST_FRONTEND_COMMAND = "fanin-frontend"
BACKGROUND_COMMANDS = (
    SENDER_COMMAND,
    SINK_COMMAND,
    INCAST_WORKER_COMMAND,
    INCAST_FRONTEND_COMMAND,
)
# Where traffic pods (``NetworkEnvBase.traffic_pod_nodes``) mount the programs.
POD_APPS_DIR = "/opt/traffic"
POD_PROGRAMS = tuple(f"{POD_APPS_DIR}/{app}.py" for app in ("flowgen", "fanout"))

_ETH = re.compile(r"^eth(\d+)$")


class TrafficProfile(BaseModel):
    """One case's background traffic. Unset fields take the defaults below."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: Pattern
    flow_sizes: Literal["websearch", "datamining", "hadoop"] = "websearch"
    # Offered load summed over every sender, in Mbit/s.
    load_mbps: float = Field(default=4.0, gt=0, le=100)
    # Flow sizes above this are clipped to it.
    max_flow_kb: int = Field(default=2048, ge=1)
    # Rate each flow is paced to; unset sends each flow as fast as TCP can.
    max_flow_kbps: float | None = Field(default=None, gt=0)
    seed: int = 1
    # ``hotspot``: number of hot receivers and their share of the load.
    hotspots: int = Field(default=1, ge=1)
    hot_fraction: float = Field(default=0.5, gt=0, lt=1)
    # ``incast``: responders per query, bytes each returns, seconds between queries.
    fan_in: int = Field(default=8, ge=2)
    incast_kb: int = Field(default=32, ge=1, le=256 * INCAST_DATAGRAM // 1024)
    incast_period_sec: float = Field(default=2.0, gt=0)
    # Period of the sine that modulates the arrival rate; unset keeps it flat.
    diurnal_period_sec: float | None = Field(default=None, gt=0)
    diurnal_amplitude: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _from_name(cls, value: Any) -> Any:
        return {"pattern": value} if isinstance(value, str) else value

    def case_value(self) -> dict[str, Any]:
        """The canonical case-row form: ``pattern`` plus non-default fields."""
        return {
            "pattern": self.pattern,
            **self.model_dump(mode="json", exclude_defaults=True, exclude={"pattern"}),
        }

    def label(self) -> str:
        """Short, stable identity for result directories and resume keys."""
        extra = self.model_dump(mode="json", exclude_defaults=True, exclude={"pattern"})
        if not extra:
            return self.pattern
        digest = zlib.crc32(repr(sorted(extra.items())).encode()) & 0xFFFFFF
        return f"{self.pattern}-{digest:06x}"


def fit_to_rate_limit(
    profile: TrafficProfile, rate_limit_kbps: int | None
) -> TrafficProfile:
    """``profile`` with its load inside ``PROFILE_RATE_SHARE`` of a send cap.

    Every flow may cross the same capped interface, so the budget bounds the
    whole lab's offered load. ``incast`` gives half to flows and half to the
    fan-in answers.
    """
    budget = rate_budget_kbps(rate_limit_kbps, PROFILE_RATE_SHARE)
    if not budget:
        return profile
    update: dict[str, Any] = {}
    flow_budget = budget / 2 if profile.pattern == "incast" else budget
    if profile.load_mbps * 1000 > flow_budget:
        update["load_mbps"] = flow_budget / 1000
    # A single elastic flow would fill the capped link on its own.
    if not profile.max_flow_kbps or profile.max_flow_kbps > flow_budget:
        update["max_flow_kbps"] = flow_budget
    if profile.pattern == "incast":
        per_query_kb = (budget / 2) * profile.incast_period_sec / 8 / profile.fan_in
        if profile.incast_kb > per_query_kb:
            update["incast_kb"] = max(1, int(per_query_kb * 1000 / 1024))
    return profile.model_copy(update=update) if update else profile


def normalize_traffic(value: Any) -> TrafficProfile | None:
    """Parse a case or CLI value: a pattern name, a mapping, or ``None``."""
    if value is None or value == "" or isinstance(value, TrafficProfile):
        return value or None
    return TrafficProfile.model_validate(value)


def traffic_field(value: Any) -> dict[str, dict[str, Any]]:
    """Return ``{"traffic": {...}}`` for a case row, or ``{}`` when unset.

    Rows without traffic omit the field so their identity stays unchanged.
    """
    profile = normalize_traffic(value)
    return {"traffic": profile.case_value()} if profile else {}


def parse_traffic_option(text: str | None) -> TrafficProfile | None:
    """Parse ``PATTERN[,KEY=VALUE...]`` from the command line."""
    if not text:
        return None
    pattern, *pairs = [part.strip() for part in text.split(",") if part.strip()]
    fields: dict[str, Any] = {"pattern": pattern}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"Traffic option {pair!r} is not KEY=VALUE.")
        fields[key.strip()] = raw.strip()
    return TrafficProfile.model_validate(fields)


@dataclass(frozen=True)
class Endpoint:
    name: str
    ip: str


Matrix = dict[tuple[str, str], float]


def _pairs(names: list[str], isolated: Callable[[str, str], bool]) -> list[tuple]:
    return [(a, b) for a in names for b in names if a != b and not isolated(a, b)]


def _random_matching(pairs: list[tuple], rng: random.Random) -> list[tuple]:
    """A maximum one-to-one sender/receiver matching, in random order."""
    options: dict[str, list[str]] = {}
    for src, dst in pairs:
        options.setdefault(src, []).append(dst)
    for dsts in options.values():
        rng.shuffle(dsts)
    sender_of: dict[str, str] = {}

    def assign(src: str, seen: set[str]) -> bool:
        for dst in options[src]:
            if dst in seen:
                continue
            seen.add(dst)
            if dst not in sender_of or assign(sender_of[dst], seen):
                sender_of[dst] = src
                return True
        return False

    sources = sorted(options)
    rng.shuffle(sources)
    for src in sources:
        assign(src, set())
    return sorted((src, dst) for dst, src in sender_of.items())


def traffic_matrix(
    profile: TrafficProfile,
    names: Iterable[str],
    *,
    isolated: Callable[[str, str], bool] = lambda a, b: False,
    demands: Mapping[tuple[str, str], float] | None = None,
) -> Matrix:
    """Pair weights summing to 1 over permitted ordered pairs of ``names``."""
    names = sorted(set(names))
    rng = random.Random(f"{profile.seed}:{profile.pattern}:matrix")
    pairs = _pairs(names, isolated)
    if not pairs:
        raise ValueError("Background traffic needs two hosts that may talk.")
    weights: Matrix
    if profile.pattern in ("uniform", "incast"):
        weights = dict.fromkeys(pairs, 1.0)
    elif profile.pattern == "gravity":
        out_mass = {n: rng.expovariate(1.0) for n in names}
        in_mass = {n: rng.expovariate(1.0) for n in names}
        weights = {(a, b): out_mass[a] * in_mass[b] for a, b in pairs}
    elif profile.pattern == "demand":
        if not demands:
            raise ValueError(
                "Traffic pattern 'demand' needs a scenario demand matrix; "
                "this scenario declares none."
            )
        permitted = set(pairs)
        weights = {
            pair: float(w) for pair, w in demands.items() if pair in permitted and w > 0
        }
    elif profile.pattern == "permutation":
        weights = dict.fromkeys(_random_matching(pairs, rng), 1.0)
    elif profile.pattern == "hotspot":
        receivers = sorted({b for _, b in pairs})
        hot = set(rng.sample(receivers, min(profile.hotspots, len(receivers))))
        hot_pairs = [p for p in pairs if p[1] in hot]
        cold_pairs = [p for p in pairs if p[1] not in hot]
        share = profile.hot_fraction if cold_pairs else 1.0
        weights = {p: share / len(hot_pairs) for p in hot_pairs}
        for p in cold_pairs:
            weights[p] = (1 - share) / len(cold_pairs)
    else:  # pragma: no cover - Literal guards the value
        raise ValueError(f"Unknown traffic pattern {profile.pattern!r}")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError(f"Traffic pattern {profile.pattern!r} left no flows.")
    return {pair: w / total for pair, w in sorted(weights.items()) if w > 0}


def _seed(profile: TrafficProfile, node: str) -> str:
    return str(zlib.crc32(f"{profile.seed}:{node}".encode()) & 0xFFFF)


def plan_background(
    profile: TrafficProfile,
    endpoints: Iterable[Endpoint],
    *,
    isolated: Callable[[str, str], bool] = lambda a, b: False,
    demands: Mapping[tuple[str, str], float] | None = None,
) -> BusinessWorkload:
    """The processes that realize ``profile`` between ``endpoints``."""
    ips = {e.name: e.ip for e in endpoints}
    matrix = traffic_matrix(profile, ips, isolated=isolated, demands=demands)
    processes: list[WorkloadProcess] = [
        WorkloadProcess(
            node=node,
            app="flowgen",
            command=SINK_COMMAND,
            args=("sink", "--port", str(FLOW_PORT)),
        )
        for node in sorted({dst for _, dst in matrix})
    ]
    rows: dict[str, list[tuple[str, float]]] = {}
    for (src, dst), weight in matrix.items():
        rows.setdefault(src, []).append((dst, weight))
    for src, row in sorted(rows.items()):
        share = sum(w for _, w in row)
        args = [
            "send",
            "--load-kbps",
            f"{profile.load_mbps * 1000 * share:.3f}",
            "--sizes",
            profile.flow_sizes,
            "--max-kb",
            str(profile.max_flow_kb),
            *(
                ("--flow-kbps", f"{profile.max_flow_kbps:g}")
                if profile.max_flow_kbps
                else ()
            ),
            "--seed",
            _seed(profile, src),
        ]
        for dst, weight in row:
            args += ["--dst", f"{ips[dst]}:{FLOW_PORT}/{weight / share:.6f}"]
        if profile.diurnal_period_sec:
            args += [
                "--diurnal-period",
                f"{profile.diurnal_period_sec:g}",
                "--diurnal-amplitude",
                f"{profile.diurnal_amplitude:g}",
            ]
        processes.append(
            WorkloadProcess(
                node=src, app="flowgen", command=SENDER_COMMAND, args=tuple(args)
            )
        )
    if profile.pattern == "incast":
        processes += _incast_processes(profile, ips, isolated)
    return BusinessWorkload(processes=tuple(processes))


def _incast_processes(
    profile: TrafficProfile,
    ips: Mapping[str, str],
    isolated: Callable[[str, str], bool],
) -> list[WorkloadProcess]:
    from nika.net_env.utils.rack_workload import fanout_processes

    rng = random.Random(f"{profile.seed}:incast")
    names = sorted(ips)
    receiver = max(
        names, key=lambda n: (sum(not isolated(n, o) for o in names), rng.random())
    )
    peers = [n for n in names if n != receiver and not isolated(n, receiver)]
    if len(peers) < 2:
        raise ValueError("Traffic pattern 'incast' needs two responders.")
    workers = sorted(rng.sample(peers, min(profile.fan_in, len(peers))))
    count = math.ceil(profile.incast_kb * 1024 / INCAST_DATAGRAM)
    return fanout_processes(
        receiver,
        {w: ips[w] for w in workers},
        count=count,
        size=INCAST_DATAGRAM,
        period=f"{profile.incast_period_sec:g}",
        port=INCAST_PORT,
        worker_command=INCAST_WORKER_COMMAND,
        aggregator_command=INCAST_FRONTEND_COMMAND,
        seed=_seed(profile, receiver),
    )


def _parse_addresses(output: str, *, skip_eth0: bool) -> str | None:
    """Pick the data-plane IPv4 address from ``ip -4 -o addr`` output."""
    from nika.telemetry import TELEMETRY_SUBNET

    telemetry = ipaddress.ip_network(TELEMETRY_SUBNET)
    found: list[tuple[int, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1].split("@", 1)[0]
        match = _ETH.match(iface)
        if not match or (skip_eth0 and iface == "eth0"):
            continue
        address = ipaddress.ip_interface(parts[3]).ip
        if address in telemetry or address.is_loopback:
            continue
        found.append((int(match.group(1)), str(address)))
    return min(found)[1] if found else None


_ADDRESSES = "ip -4 -o addr show scope global"
_DISCOVER = f"command -v python3 >/dev/null 2>&1 || exit 0; {_ADDRESSES}"


def discover_endpoints(
    runtime: "LabRuntime",
    hosts: Iterable[str],
    *,
    python_elsewhere: Iterable[str] = (),
) -> list[Endpoint]:
    """Hosts that can run the programs, with their data-plane IPv4 address.

    Hosts in ``python_elsewhere`` run them outside the node's own userspace
    (``NetworkEnvBase.traffic_pod_nodes``), so they need no Python.
    """
    hosts = sorted(set(hosts))
    elsewhere = set(python_elsewhere)
    # Containerlab gives every node a management eth0; data links start at eth1.
    skip_eth0 = runtime.backend == "containerlab"

    def probe(host: str) -> Endpoint | None:
        script = _ADDRESSES if host in elsewhere else _DISCOVER
        ip = _parse_addresses(runtime.exec(host, script), skip_eth0=skip_eth0)
        return Endpoint(host, ip) if ip else None

    return [e for e in bounded_parallel_map(probe, hosts) if e is not None]


def endpoint_names(net_env: "NetworkEnvBase") -> list[str]:
    """Hosts, reachability targets, and service nodes of the scenario."""
    from nika.runtime.spec import MachineInventory

    inventory = MachineInventory(net_env.machine_identities)
    names = set(net_env.hosts or ()) | set(inventory.reachability_targets())
    for servers in inventory.services().values():
        names.update(servers)
    return sorted(names)


def stop_background_traffic(net_env: "NetworkEnvBase") -> None:
    """Stop the programs an earlier profile left on a reused lab."""
    import shlex

    runtime = net_env._build_runtime()
    pods = net_env.traffic_pod_nodes()
    alternatives = "|".join(BACKGROUND_COMMANDS)
    pattern = shlex.quote(f"^python3 /usr/local/bin/({alternatives})( |$)")
    hosts = [name for name in endpoint_names(net_env) if name not in pods]
    bounded_parallel_map(
        lambda host: runtime.exec(host, f"pkill -f {pattern} || true"), hosts
    )
    net_env.stop_traffic_programs()


def start_background_traffic(
    net_env: "NetworkEnvBase", profile: TrafficProfile
) -> tuple[TrafficProfile, BusinessWorkload]:
    """Plan ``profile`` on the deployed lab and start it (idempotent).

    Returns the profile as run, after ``fit_to_rate_limit``, and its programs.
    """
    limits = [
        net_env.router_rate_limit_kbps(),
        # Scenarios that shape their paths below the router cap say so.
        type(net_env).path_rate_limit_kbps(getattr(net_env, "device_profile", None)),
    ]
    profile = fit_to_rate_limit(
        profile, min((limit for limit in limits if limit), default=None)
    )
    runtime = net_env._build_runtime()
    names = endpoint_names(net_env)
    endpoints = discover_endpoints(
        runtime, names, python_elsewhere=net_env.traffic_pod_nodes()
    )
    if len(endpoints) < 2:
        missing = sorted(set(names) - {e.name for e in endpoints})
        raise ValueError(
            f"Background traffic needs two hosts with a data-plane IPv4 address "
            f"and Python; {net_env.name} has {len(endpoints)}. Without them: "
            f"{', '.join(missing) or 'none'}."
        )
    intent = net_env.probe_intent()
    workload = plan_background(
        profile,
        endpoints,
        isolated=intent.isolation_matcher() if intent else (lambda a, b: False),
        demands=net_env.traffic_demands() if profile.pattern == "demand" else None,
    )
    net_env.start_traffic_programs(workload)
    return profile, workload
