"""Agent-view audit stages: what a diagnosing agent reads on the lab.

Two deterministic checks, run by ``python -m experiment.audit.matrix``:

- ``agent_view`` (healthy cases) and ``baseline_agent_view`` (the healthy
  lab of a fault case, before inject): run the PingMesh tool with the default
  arguments an agent uses, the scenario's own probe path, and differential
  probes (IPv4 and IPv6 endpoint mesh, every published DNS name from every
  resolver client, FRR BGP peer state, Kubernetes Warning events), each twice
  ``ROUND_GAP_SEC`` apart. A check that fails in both rounds is something an
  agent would read as a fault on a healthy lab, and fails the stage.
- ``leak_scan`` (fault cases): snapshot the state an agent can read on every
  node (files under ``SCAN_DIRS``, processes, listening sockets, links,
  routes, rules, firewall, tc, FRR, OVS, SR Linux, Kubernetes objects, and
  P4Runtime tables) right before and right after inject, diff them with
  per-deployment values masked, and fail on an added or changed artifact whose
  name or content names NIKA or the fault, or that is a copy of pre-fault state.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

Exec = Callable[[str, str, float], str]

ROUND_GAP_SEC = 15.0
WORKERS = 16
MAX_PING_PAIRS = 400
MAX_DNS_CLIENTS = 6
MAX_DNS_NAMES = 40
MGMT_IFACES = frozenset({"eth0", "mgmt0"})
EVIDENCE_CAP = 40
# Probe groups that list a finding only in the rounds where it appears.
LISTED_ONLY_WHEN_SEEN = frozenset({"pingmesh", "k8s"})

# ---------------------------------------------------------------------------
# Round classification (shared by every differential probe)
# ---------------------------------------------------------------------------


def classify_rounds(
    rounds: list[dict[str, bool | None]], *, missing: bool | None = None
) -> dict[str, str]:
    """Combine per-round check results into holds, violated, flaky, or error.

    ``True`` holds, ``False`` fails, ``None`` could not be measured. A check
    absent from a round reads as ``missing``: ``None`` for checks every round
    runs, ``True`` for findings that are listed only when they appear (a
    PingMesh anomaly, a Warning event).
    """
    keys = sorted({key for result in rounds for key in result})
    outcome: dict[str, str] = {}
    for key in keys:
        values = [result.get(key, missing) for result in rounds]
        if any(value is None for value in values):
            outcome[key] = "error"
        elif all(values):
            outcome[key] = "holds"
        elif not any(values):
            outcome[key] = "violated"
        else:
            outcome[key] = "flaky"
    return outcome


# ---------------------------------------------------------------------------
# Healthy agent view: differential probes
# ---------------------------------------------------------------------------


def parse_global_addresses(
    output: str, *, skip: Iterable[str] = ()
) -> dict[int, list[str]]:
    """Global IPv4 and IPv6 addresses from ``ip -o addr show scope global``.

    Ordered by interface name, so the first address of a multi-homed node is
    its ``eth0`` endpoint address (the one PingMesh probes) rather than
    whichever interface the kernel numbered first.
    """
    skipped = set(skip) | {"lo"}
    found: dict[int, list[str]] = {4: [], 6: []}
    rows: list[tuple[list[Any], int, str]] = []
    for line in output.splitlines():
        match = re.match(r"\d+:\s+(\S+?)(?:@\S+)?\s+(inet6?)\s+(\S+)", line)
        if not match or match.group(1) in skipped:
            continue
        family = 6 if match.group(2) == "inet6" else 4
        iface = [
            int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", match.group(1))
        ]
        rows.append((iface, family, match.group(3).split("/")[0]))
    for _, family, address in sorted(rows, key=lambda row: (row[0], row[1])):
        found[family].append(address)
    return found


def mesh_pairs(nodes: list[str]) -> list[tuple[str, str]]:
    """All ordered pairs, or a deterministic rotation subset above the cap."""
    every = [(a, b) for a in nodes for b in nodes if a != b]
    if len(every) <= MAX_PING_PAIRS:
        return every
    per_source = max(1, MAX_PING_PAIRS // len(nodes))
    return [
        (nodes[i], nodes[(i + k) % len(nodes)])
        for i in range(len(nodes))
        for k in range(1, per_source + 1)
    ]


def ping_batch_command(targets: list[str], family: int) -> str:
    """One exec that pings every target of a source in parallel."""
    flag = "-6 " if family == 6 else ""
    jobs = " ".join(
        f"(o=$(ping {flag}-c 3 -W 1 -q {shlex.quote(t)} 2>&1); "
        f"r=$(printf '%s' \"$o\" | grep -Eo '[0-9]+ (packets )?received' | head -n1); "
        f'echo "@@ {t} ${{r:-none}}") &'
        for t in targets
    )
    return (
        f"command -v ping >/dev/null 2>&1 || {{ echo @@NOTOOL; exit 0; }}; {jobs} wait"
    )


def parse_ping_batch(output: str, targets: list[str]) -> dict[str, bool | None]:
    """Target -> reachable (any echo reply), ``None`` when not measured."""
    if "@@NOTOOL" in output:
        return dict.fromkeys(targets)
    seen: dict[str, bool | None] = {}
    for line in output.splitlines():
        match = re.match(r"@@ (\S+) (?:(\d+) (?:packets )?received|none)", line.strip())
        if match:
            count = match.group(2)
            seen[match.group(1)] = None if count is None else int(count) > 0
    return {target: seen.get(target) for target in targets}


def resolve_batch_command(names: list[str]) -> str:
    jobs = " ".join(
        f"(r=$(getent hosts {shlex.quote(n)} 2>/dev/null | head -n1); "
        f'echo "@@ {n} ${{r:-__NO_ANSWER__}}") &'
        for n in names
    )
    return (
        "command -v getent >/dev/null 2>&1 || { echo @@NOTOOL; exit 0; }; "
        f"{jobs} wait"
    )


def parse_resolve_batch(output: str, names: list[str]) -> dict[str, bool | None]:
    if "@@NOTOOL" in output:
        return dict.fromkeys(names)
    seen: dict[str, bool] = {}
    for line in output.splitlines():
        match = re.match(r"@@ (\S+) (.*)", line.strip())
        if match:
            seen[match.group(1)] = "__NO_ANSWER__" not in match.group(2)
    return {name: seen.get(name) for name in names}


def bgp_peer_states(output: str) -> dict[str, str]:
    """``vrf/peer`` -> state from FRR ``show bgp vrf all summary json``."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {}
    states: dict[str, str] = {}

    def walk(node: Any, vrf: str) -> None:
        if not isinstance(node, dict):
            return
        peers = node.get("peers")
        if isinstance(peers, dict):
            for peer, info in peers.items():
                if isinstance(info, dict):
                    states[f"{vrf}/{peer}"] = str(info.get("state", "unknown"))
        for key, value in node.items():
            if key != "peers" and isinstance(value, dict):
                walk(value, vrf if vrf else key)

    if isinstance(data, dict):
        # Single-VRF output has the AFI keys at the top level.
        if any(key.endswith("Unicast") for key in data):
            walk(data, "default")
        else:
            for vrf, body in data.items():
                walk(body, vrf)
    return states


def zone_names(files: dict[str, str]) -> list[str]:
    """Published A/AAAA names from BIND zone files ``{path: text}``."""
    record = re.compile(r"^(\S+)\s+(?:\d+\s+)?(?:IN\s+)?(?:A|AAAA)\s+(\S+)", re.I)
    names: set[str] = set()
    for path, text in sorted(files.items()):
        origin = Path(path).name[3:].rstrip(".")
        if origin in {"root", "0", "127", "255", "empty"}:
            continue
        for line in text.splitlines():
            if line.upper().startswith("$ORIGIN"):
                origin = line.split()[1].rstrip(".")
                continue
            match = record.match(line)
            if not match or line.startswith((";", " ", "\t")):
                continue
            # BIND's stock localhost zones (db.local) publish loopback only.
            if match.group(2).startswith(("127.", "::1")):
                continue
            name = match.group(1)
            if name == "@":
                fqdn = origin
            elif name.endswith("."):
                fqdn = name.rstrip(".")
            else:
                fqdn = f"{name}.{origin}"
            names.add(fqdn)
    ordered = sorted(names)
    if len(ordered) <= MAX_DNS_NAMES:
        return ordered
    step = len(ordered) / MAX_DNS_NAMES
    return sorted(
        {ordered[int(i * step)] for i in range(MAX_DNS_NAMES)} | {ordered[-1]}
    )


def _lab_zone_files(net_env: Any) -> dict[str, str]:
    """Zone files the deployment was built from (Kathara lab filesystem)."""
    files: dict[str, str] = {}
    fs = getattr(getattr(net_env, "lab", None), "fs", None)
    if fs is not None:
        for name in sorted(fs.walk.files()):
            if Path(name).name.startswith("db."):
                try:
                    files[name] = fs.readtext(name)
                except Exception:  # noqa: BLE001 - binary or unreadable file
                    continue
    topology = getattr(net_env, "topology_file", None)
    if topology and Path(topology).is_file():
        for path in Path(topology).parent.rglob("db.*"):
            files[str(path)] = path.read_text(encoding="utf-8", errors="replace")
    return files


def _pmap(func: Callable[[Any], Any], items: list[Any]) -> list[Any]:
    if not items:
        return []
    with ThreadPoolExecutor(min(WORKERS, len(items))) as pool:
        return list(pool.map(func, items))


def _safe_exec(exec_cmd: Exec, node: str, command: str, timeout: float) -> str | None:
    try:
        output = str(exec_cmd(node, command, timeout) or "")
    except Exception:  # noqa: BLE001 - a node that cannot be read is not measured
        return None
    return None if output.startswith("[TIMEOUT]") else output


class AgentViewProbe:
    """Differential probes over one deployed lab, plus the agent's default tools."""

    def __init__(
        self,
        exec_cmd: Exec,
        *,
        machines: dict[str, Any],
        endpoints: list[str],
        backend: str,
        isolated: Callable[[str, str], bool],
        zone_files: dict[str, str],
        pingmesh: Callable[[], dict[str, Any]] | None = None,
        path_probe: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.exec = exec_cmd
        self.machines = machines
        self.endpoints = endpoints
        self.backend = backend
        self.isolated = isolated
        self.names = zone_names(zone_files)
        self.pingmesh = pingmesh
        self.path_probe = path_probe
        self.addresses: dict[str, dict[int, list[str]]] = {}
        self.resolvers: list[str] = []

    def _caps(self, node: str) -> set[str]:
        identity = self.machines.get(node)
        return set(getattr(identity, "capabilities", ()) or ())

    def prepare(self) -> None:
        skip = MGMT_IFACES if self.backend == "containerlab" else ()
        outputs = _pmap(
            lambda n: (
                n,
                _safe_exec(
                    self.exec,
                    n,
                    "ip -o addr show scope global 2>/dev/null; echo @@RESOLV; "
                    "cat /etc/resolv.conf 2>/dev/null",
                    20,
                ),
            ),
            self.endpoints,
        )
        for node, output in outputs:
            text = output or ""
            addr_text, _, resolv = text.partition("@@RESOLV")
            self.addresses[node] = parse_global_addresses(addr_text, skip=skip)
            if re.search(r"^nameserver\s", resolv, re.M):
                self.resolvers.append(node)
        self.resolvers = sorted(self.resolvers)[:MAX_DNS_CLIENTS]

    def _ping_round(self, family: int) -> dict[str, bool | None]:
        nodes = [n for n in self.endpoints if self.addresses.get(n, {}).get(family)]
        by_source: dict[str, list[tuple[str, str]]] = {}
        for src, dst in mesh_pairs(nodes):
            by_source.setdefault(src, []).append((dst, self.addresses[dst][family][0]))

        def run(src: str) -> dict[str, bool | None]:
            targets = by_source[src]
            ips = sorted({ip for _, ip in targets})
            output = _safe_exec(self.exec, src, ping_batch_command(ips, family), 40)
            reached = (
                dict.fromkeys(ips) if output is None else parse_ping_batch(output, ips)
            )
            out: dict[str, bool | None] = {}
            for dst, ip in targets:
                value = reached.get(ip)
                if value is not None and self.isolated(src, dst):
                    # A pair the design isolates must stay unreachable.
                    value = not value
                tag = " (isolated by design)" if self.isolated(src, dst) else ""
                out[f"ping{family} {src} -> {dst} {ip}{tag}"] = value
            return out

        merged: dict[str, bool | None] = {}
        for part in _pmap(run, sorted(by_source)):
            merged.update(part)
        return merged

    def _dns_round(self) -> dict[str, bool | None]:
        if not self.names:
            return {}

        def run(node: str) -> dict[str, bool | None]:
            output = _safe_exec(self.exec, node, resolve_batch_command(self.names), 40)
            got = (
                dict.fromkeys(self.names)
                if output is None
                else parse_resolve_batch(output, self.names)
            )
            return {f"dns {node} resolve {name}": got[name] for name in self.names}

        merged: dict[str, bool | None] = {}
        for part in _pmap(run, self.resolvers):
            merged.update(part)
        return merged

    def _bgp_round(self) -> dict[str, bool | None]:
        routers = sorted(n for n in self.machines if "frr" in self._caps(n))

        def run(node: str) -> dict[str, bool | None]:
            output = _safe_exec(
                self.exec, node, "vtysh -c 'show bgp vrf all summary json'", 20
            )
            if output is None:
                return {f"bgp {node}": None}
            return {
                f"bgp {node} peer {peer} Established": state == "Established"
                for peer, state in bgp_peer_states(output).items()
            }

        merged: dict[str, bool | None] = {}
        for part in _pmap(run, routers):
            merged.update(part)
        return merged

    def _k8s_round(self) -> dict[str, bool | None]:
        nodes = sorted(n for n in self.machines if "k3s" in self._caps(n))
        controller = next((n for n in nodes if "controller" in n), None) or (
            nodes[0] if nodes else None
        )
        if controller is None:
            return {}
        output = _safe_exec(
            self.exec,
            controller,
            "kubectl --request-timeout=20s get events -A --field-selector type=Warning "
            "--no-headers -o custom-columns="
            "NS:.metadata.namespace,KIND:.involvedObject.kind,"
            "NAME:.involvedObject.name,REASON:.reason 2>&1",
            30,
        )
        if output is None or output.lstrip().lower().startswith(
            ("error", "the connection", "unable")
        ):
            return {"k8s warning events readable": None}
        events = sorted(
            {" ".join(line.split()) for line in output.splitlines() if line.strip()}
            - {"No resources found"}
        )
        events = [e for e in events if not e.lower().startswith("no resources found")]
        return {f"k8s no Warning event: {event}": False for event in events}

    def _pingmesh_round(self) -> tuple[dict[str, bool | None], dict[str, Any]]:
        if self.pingmesh is None:
            return {}, {}
        try:
            snapshot = self.pingmesh()
        except Exception as exc:  # noqa: BLE001 - recorded as not measured
            return {"pingmesh ran": None}, {"error": f"{type(exc).__name__}: {exc}"}
        results = {
            f"pingmesh {a['type']} {a['source']} -> {a['target']}": False
            for a in snapshot.get("anomalies") or []
        }
        if snapshot.get("anomalies_omitted"):
            results["pingmesh anomalies omitted"] = False
        summary = dict(snapshot.get("summary") or {})
        summary["anomalies"] = [
            f"{a['type']} {a['source']} -> {a['target']}: {a['detail']}"
            for a in (snapshot.get("anomalies") or [])[:EVIDENCE_CAP]
        ]
        return results, summary

    def run_round(self) -> tuple[dict[str, dict[str, bool | None]], dict[str, Any]]:
        mesh, summary = self._pingmesh_round()
        path: dict[str, bool | None] = {}
        path_evidence: dict[str, Any] = {}
        if self.path_probe is not None:
            try:
                path_evidence = self.path_probe()
            except Exception as exc:  # noqa: BLE001 - recorded as not measured
                path_evidence = {"error": f"{type(exc).__name__}: {exc}"}
            for key in ("ping_ok", "http_ok"):
                if key in path_evidence:
                    path[f"scenario probe path {key}"] = path_evidence[key]
            if "error" in path_evidence:
                path["scenario probe path"] = None
        groups = {
            "pingmesh": mesh,
            "probe_path": path,
            "ping4": self._ping_round(4),
            "ping6": self._ping_round(6),
            "dns": self._dns_round(),
            "bgp": self._bgp_round(),
            "k8s": self._k8s_round(),
        }
        return groups, {"pingmesh_summary": summary, "probe_path": path_evidence}

    def run(self, rounds: int = 2) -> dict[str, Any]:
        started = time.monotonic()
        self.prepare()
        per_round: list[dict[str, dict[str, bool | None]]] = []
        extras: list[dict[str, Any]] = []
        for index in range(rounds):
            if index:
                time.sleep(ROUND_GAP_SEC)
            groups, extra = self.run_round()
            per_round.append(groups)
            extras.append(extra)
        return summarize_agent_view(
            per_round,
            extras,
            endpoints=self.endpoints,
            addresses=self.addresses,
            resolvers=self.resolvers,
            names=self.names,
            duration=time.monotonic() - started,
        )


def summarize_agent_view(
    per_round: list[dict[str, dict[str, bool | None]]],
    extras: list[dict[str, Any]],
    **context: Any,
) -> dict[str, Any]:
    """Group check outcomes; ``violated`` entries are what fails the stage."""
    names = sorted({name for groups in per_round for name in groups})
    checks: dict[str, int] = {}
    found: dict[str, list[str]] = {"violated": [], "flaky": [], "error": []}
    for name in names:
        outcome = classify_rounds(
            [groups.get(name, {}) for groups in per_round],
            missing=True if name in LISTED_ONLY_WHEN_SEEN else None,
        )
        checks[name] = len(outcome)
        for key, value in outcome.items():
            if value in found:
                found[value].append(key)
    addresses = context.get("addresses") or {}
    return {
        "checks": checks,
        "violated": found["violated"][:EVIDENCE_CAP],
        "violated_count": len(found["violated"]),
        "flaky": found["flaky"][:EVIDENCE_CAP],
        "flaky_count": len(found["flaky"]),
        "error": found["error"][:EVIDENCE_CAP],
        "error_count": len(found["error"]),
        "endpoints": len(context.get("endpoints") or []),
        "ipv4_endpoints": sum(1 for a in addresses.values() if a.get(4)),
        "ipv6_endpoints": sum(1 for a in addresses.values() if a.get(6)),
        "dns_clients": context.get("resolvers") or [],
        "dns_names": len(context.get("names") or []),
        "pingmesh_summary": [extra.get("pingmesh_summary") for extra in extras],
        "probe_path": [extra.get("probe_path") for extra in extras],
        "rounds": len(per_round),
        "round_gap_sec": ROUND_GAP_SEC,
        "duration_sec": round(float(context.get("duration") or 0), 1),
    }


# ---------------------------------------------------------------------------
# Injection leak scan
# ---------------------------------------------------------------------------

SCAN_DIRS = ("/etc", "/tmp", "/var", "/opt", "/root", "/usr/local")
# Package, container-runtime, and cache trees: large, and not where an
# injector writes.
PRUNE_DIRS = (
    "/var/lib/rancher",
    "/var/lib/containerd",
    "/var/lib/kubelet",
    "/var/lib/docker",
    "/var/lib/apt",
    "/var/lib/dpkg",
    "/var/cache",
    "/var/log/journal",
    # Volatile daemon runtime state (FRR per-daemon crash/log ring buffers
    # ``<daemon>.<pid>/logbuf.<n>``), rewritten by the daemons themselves.
    "/var/tmp/frr",
    "/usr/local/lib",
    "/usr/local/include",
    "/usr/local/share",
)
MAX_FILE_KIB = 1024
# Tokens that name the harness or an injection regardless of the fault, plus
# synthetic tools, effect names, and placeholder values no operator deploys.
GENERIC_TOKENS = (
    "nika",
    "(^|[^a-z])inject",
    "(^|[^a-z])fault",
    "stress-ng",
    "bitflip",
    "drop_all",
    "(^|[^a-z])chaos",
    "00:11:22:33:44:55",
)
BACKUP_NAME = re.compile(
    r"(\.(sav|bak|orig|old|backup|save|prev|pre[-_.]?[a-z0-9]*)|~)$", re.I
)
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
# Word pairs of a fault id that are also native keywords of the tools the
# fault configures (nft's ICMP code ``frag-needed``); the rule is the fault.
NATIVE_KEYWORDS = frozenset({"frag[-_]needed"})
SNAPSHOT_MARK = "__agentview_snapshot__"


def fault_tokens(fault: str) -> list[str]:
    """Regex alternatives that name ``fault`` (registry id and its phrases).

    Every run of two or more consecutive words of the id, joined by ``_`` or
    ``-``, plus the whole id without separators when it has three or more
    words (class names such as ``HostIncorrectDns``). Single words are left
    out: ``dns`` or ``loss`` appear in any healthy config.
    """
    words = [w for w in fault.lower().split("_") if w]
    tokens: list[str] = []
    for size in range(len(words), 1, -1):
        for start in range(len(words) - size + 1):
            tokens.append("[-_]".join(words[start : start + size]))
    if len(words) >= 3:
        tokens.append("".join(words))
    return [t for t in dict.fromkeys(tokens) if t not in NATIVE_KEYWORDS]


def token_pattern(fault: str) -> str:
    """Case-insensitive ERE (also valid Python ``re``) for leak tokens."""
    return "|".join([*GENERIC_TOKENS, *fault_tokens(fault)])


def mask_deployment(text: str) -> str:
    """Mask per-deployment values line by line.

    Timestamps, PID suffixes such as ``isisd.68``, and on timestamped lines
    every standalone number. Config values such as an ASN or prefix are kept.
    """
    lines = []
    for line in re.split(r"\n|\\n", _PID_SUFFIX.sub(".<n>", text.lower())):
        line = _TIMESTAMP.sub("<time>", line)
        if "<time>" in line:
            line = _STANDALONE_NUMBER.sub("<n>", line)
        lines.append(line)
    return "\n".join(lines)


_TIMESTAMP = re.compile(
    r"\d{4}[-/]\d{2}[-/]\d{2}[ t]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(z|[ ]?[+-]\d{2}:?\d{2})?"
    r"|\b((mon|tue|wed|thu|fri|sat|sun) +)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r" +\d{1,2} +(\d{2}:\d{2}(:\d{2})?|\d{4})\b"
)
_PID_SUFFIX = re.compile(r"(?<=[a-z])\.\d+\b")
_STANDALONE_NUMBER = re.compile(r"(?<![.:/-])\b\d+\b(?![.:/]\d)")
# Counters, ages, kernel indexes, and object versions that change on their own.
_VOLATILE = (
    (re.compile(r"\b(pid|fd|ino)=\d+"), r"\1=<n>"),
    (re.compile(r"\[\d+:\d+\]"), "[<n>:<n>]"),
    (re.compile(r"\b(n_packets|n_bytes|idle_age|hard_age)=\d+"), r"\1=<n>"),
    (re.compile(r"\bduration=[\d.]+s"), "duration=<n>s"),
    (re.compile(r"\bpackets \d+ bytes \d+"), "packets <n> bytes <n>"),
    (re.compile(r"@if\d+"), "@if<n>"),
    (re.compile(r"\bexpires \d+sec"), "expires <n>sec"),
    (
        re.compile(
            r"\b(resourceversion|uid|generation|observedgeneration)\b(\"?:\s*)\S+"
        ),
        r"\1\2<v>",
    ),
    (
        re.compile(
            r"\"(packets|bytes|packet_count|byte_count|count|value)\"(\s*:\s*)\d+"
        ),
        r'"\1"\2<n>',
    ),
)


def mask_line(line: str) -> str:
    """Mask one collected state line so only meaningful changes diff."""
    masked = mask_deployment(line)
    for pattern, repl in _VOLATILE:
        masked = pattern.sub(repl, masked)
    return " ".join(masked.split())


def snapshot_script(pattern: str, capabilities: set[str]) -> str:
    """One shell script that prints every agent-visible section of a node."""
    prune = " -o ".join(f"-path {d}" for d in PRUNE_DIRS)
    find = (
        f"find {' '.join(SCAN_DIRS)} \\( {prune} \\) -prune -o "
        f"-type f -size -{MAX_FILE_KIB}k -print0 2>/dev/null"
    )
    quoted = shlex.quote(pattern)
    sections = [
        ("files", f"{find} | xargs -0 -r md5sum 2>/dev/null"),
        (
            "hits",
            f"{find} | xargs -0 -r grep -c -H -i -E {quoted} 2>/dev/null"
            " | grep -v ':0$'",
        ),
        ("nika_files", f"{find} | xargs -0 -r grep -l -i nika 2>/dev/null"),
        (
            "symlinks",
            f"find {' '.join(SCAN_DIRS)} \\( {prune} \\) -prune -o -type l -print "
            '2>/dev/null | while read -r l; do echo "$l -> $(readlink "$l")"; done',
        ),
        ("processes", "ps -eo user,args 2>/dev/null || ps -o user,args"),
        ("listen", "ss -lntupH 2>/dev/null || netstat -lntup 2>/dev/null"),
        ("links", "ip -d link show 2>/dev/null"),
        ("addresses", "ip -br addr 2>/dev/null"),
        (
            "routes",
            "ip route show table all 2>/dev/null; ip -6 route show table all 2>/dev/null",
        ),
        ("rules", "ip rule 2>/dev/null; ip -6 rule 2>/dev/null"),
        (
            "firewall",
            "nft list ruleset 2>/dev/null; iptables-save 2>/dev/null; "
            "ip6tables-save 2>/dev/null",
        ),
        (
            "tc",
            "tc qdisc show 2>/dev/null; for d in $(ls /sys/class/net 2>/dev/null); do "
            "tc filter show dev $d 2>/dev/null; tc filter show dev $d ingress 2>/dev/null; "
            "tc filter show dev $d egress 2>/dev/null; done",
        ),
    ]
    if "frr" in capabilities:
        sections.append(("frr", "vtysh -c 'show running-config' 2>/dev/null"))
    if "ovs" in capabilities or "openflow" in capabilities:
        sections.append(
            (
                "ovs",
                "ovs-vsctl show 2>/dev/null; for b in $(ovs-vsctl list-br 2>/dev/null); "
                "do ovs-ofctl -O OpenFlow13 dump-flows $b 2>/dev/null; done",
            )
        )
    if "nokia_srlinux" in capabilities or "srl" in capabilities:
        sections.append(("srl", "sr_cli -d 'info flat' 2>/dev/null"))
    if "k3s" in capabilities:
        sections.append(
            (
                "k8s",
                "kubectl --request-timeout=20s get "
                "all,configmaps,networkpolicies,ingresses,serviceaccounts,roles,rolebindings "
                "-A -o yaml 2>/dev/null | grep -v -E "
                "'^\\s*(creationTimestamp|resourceVersion|uid|time|lastTransitionTime|"
                "startedAt|lastProbeTime|lastUpdateTime|podIP|ip|hostIP):'; "
                "kubectl --request-timeout=20s get events -A --no-headers "
                "-o custom-columns=NS:.metadata.namespace,TYPE:.type,"
                "OBJ:.involvedObject.name,REASON:.reason,MSG:.message 2>/dev/null",
            )
        )
    body = "; ".join(f"echo '@@@{name}'; {command}" for name, command in sections)
    return f": {SNAPSHOT_MARK}; {body}"


def parse_snapshot(output: str) -> dict[str, Any]:
    """Split a node snapshot into its file manifest, token hits, and text sections."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("@@@"):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    files: dict[str, str] = {}
    for line in sections.pop("files", []):
        match = re.match(r"([0-9a-f]{32})\s+\*?(.+)$", line)
        if match:
            files[match.group(2)] = match.group(1)
    hits: dict[str, int] = {}
    for line in sections.pop("hits", []):
        path, _, count = line.rpartition(":")
        if path and count.isdigit():
            hits[path] = int(count)
    nika_files = [line for line in sections.pop("nika_files", []) if line.strip()]
    text = {
        name: [
            mask_line(line)
            for line in lines
            if line.strip() and SNAPSHOT_MARK not in line
        ]
        for name, lines in sections.items()
    }
    return {"files": files, "hits": hits, "nika_files": nika_files, "text": text}


def _is_copy_of(path: str, original: str) -> bool:
    """Whether an added file looks like a deliberate copy of ``original``.

    Identical content alone also matches routine runtime buffers, so the copy
    must sit in a configuration tree or carry the original's file name.
    """
    return path.startswith("/etc/") or Path(original).name in path


def diff_snapshots(
    before: dict[str, Any], after: dict[str, Any], pattern: str
) -> dict[str, Any]:
    """Diff one node's snapshots and flag fault-revealing changes.

    An added file is flagged when its path names a token; an added or changed
    file when it holds more token matches than before, or when it is a backup of pre-fault
    state: its name ends like a backup, or its content equals the pre-inject
    content of a file that changed or disappeared. An added line in a text
    section is flagged when it names a token.
    """
    token = re.compile(pattern, re.I)
    old, new = before.get("files", {}), after.get("files", {})
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    prior = {old[p]: p for p in [*changed, *removed] if old[p] != EMPTY_MD5}
    flagged: list[dict[str, Any]] = []
    for path in [*added, *changed]:
        why: list[str] = []
        # A file that existed before inject showed its name already.
        if path in added and token.search(path):
            why.append("name_token")
        if after.get("hits", {}).get(path, 0) > before.get("hits", {}).get(path, 0):
            why.append("content_token")
        if path in added and BACKUP_NAME.search(path):
            why.append("backup_name")
        if path in added and new[path] in prior and _is_copy_of(path, prior[new[path]]):
            why.append(f"copy_of_prefault:{prior[new[path]]}")
        if why:
            flagged.append(
                {
                    "section": "files",
                    "path": path,
                    "change": "added" if path in added else "changed",
                    "why": why,
                }
            )
    lines_added: dict[str, int] = {}
    new_processes: list[str] = []
    before_text, after_text = before.get("text", {}), after.get("text", {})
    for section in sorted(set(before_text) | set(after_text)):
        delta = Counter(after_text.get(section, [])) - Counter(
            before_text.get(section, [])
        )
        if delta:
            lines_added[section] = sum(delta.values())
        for line in sorted(delta):
            if section == "processes":
                new_processes.append(line)
            if token.search(line):
                flagged.append(
                    {
                        "section": section,
                        "line": line[:300],
                        "change": "added",
                        "why": ["line_token"],
                    }
                )
    return {
        "flagged": flagged,
        "files_added": added,
        "files_changed": changed,
        "files_removed": removed,
        "lines_added": lines_added,
        "new_processes": new_processes,
    }


def prefault_mentions(state: dict[str, dict[str, Any] | None]) -> list[str]:
    """Agent-visible ``nika`` mentions that were there before inject.

    Inject did not add them and they name no fault, so they do not fail the
    scan, but they show in every trial of the scenario.
    """
    found: list[str] = []
    for node, snap in sorted(state.items()):
        if not snap:
            continue
        paths = [p for p in snap.get("files", {}) if "nika" in p.lower()]
        found += [
            f"{node}:{path}" for path in sorted({*paths, *snap.get("nika_files", [])})
        ]
        for section, lines in sorted(snap["text"].items()):
            found += [
                f"{node}:{section}: {line[:160]}"
                for line in dict.fromkeys(lines)
                if "nika" in line
            ]
    return found


class LeakScan:
    """Snapshot agent-visible state before inject and diff it after inject."""

    def __init__(
        self,
        exec_cmd: Exec,
        *,
        machines: dict[str, Any],
        fault: str,
        p4_read: Callable[[], str] | None = None,
    ) -> None:
        self.exec = exec_cmd
        self.machines = machines
        self.pattern = token_pattern(fault)
        self.p4_read = p4_read
        self.before: dict[str, dict[str, Any] | None] = {}

    def _capture(self) -> dict[str, dict[str, Any] | None]:
        def one(node: str) -> tuple[str, dict[str, Any] | None]:
            caps = set(getattr(self.machines[node], "capabilities", ()) or ())
            output = _safe_exec(
                self.exec, node, snapshot_script(self.pattern, caps), 120
            )
            return node, None if output is None else parse_snapshot(output)

        state = dict(_pmap(one, sorted(self.machines)))
        if self.p4_read is not None:
            try:
                text = self.p4_read()
                state["p4runtime"] = {
                    "files": {},
                    "hits": {},
                    "text": {
                        "p4runtime": [
                            mask_line(x) for x in text.splitlines() if x.strip()
                        ]
                    },
                }
            except Exception:  # noqa: BLE001 - the table read is optional evidence
                state["p4runtime"] = None
        return state

    def capture_before(self) -> None:
        self.before = self._capture()

    def _matching_lines(self, node: str, paths: list[str]) -> dict[str, list[str]]:
        if not paths or node == "p4runtime":
            return {}
        quoted = shlex.quote(self.pattern)
        command = "; ".join(
            f"echo @@@{shlex.quote(p)}; grep -n -i -E {quoted} {shlex.quote(p)} "
            "2>/dev/null | head -n 3 | cut -c1-200"
            for p in paths[:20]
        )
        output = _safe_exec(self.exec, node, command, 30) or ""
        found: dict[str, list[str]] = {}
        current = None
        for line in output.splitlines():
            if line.startswith("@@@"):
                current = line[3:]
                found[current] = []
            elif current is not None:
                found[current].append(line)
        return found

    def capture_after_and_diff(self) -> dict[str, Any]:
        after = self._capture()
        flagged: list[dict[str, Any]] = []
        changes: dict[str, Any] = {}
        unreadable: list[str] = []
        new_processes: list[str] = []
        for node in sorted(set(self.before) | set(after)):
            old, new = self.before.get(node), after.get(node)
            if old is None or new is None:
                unreadable.append(node)
                continue
            diff = diff_snapshots(old, new, self.pattern)
            content = [
                item["path"]
                for item in diff["flagged"]
                if item["section"] == "files" and "content_token" in item["why"]
            ]
            lines = self._matching_lines(node, content)
            for item in diff["flagged"]:
                item["node"] = node
                if item.get("path") in lines:
                    item["lines"] = lines[item["path"]]
                flagged.append(item)
            new_processes += [f"{node}: {line}" for line in diff["new_processes"]]
            if diff["files_added"] or diff["files_changed"] or diff["lines_added"]:
                changes[node] = {
                    "files_added": diff["files_added"][:10],
                    "files_added_count": len(diff["files_added"]),
                    "files_changed": diff["files_changed"][:10],
                    "files_changed_count": len(diff["files_changed"]),
                    "files_removed_count": len(diff["files_removed"]),
                    "lines_added": diff["lines_added"],
                }
        return {
            "pattern": self.pattern,
            "prefault_mentions": prefault_mentions(self.before)[:EVIDENCE_CAP],
            "scan_dirs": list(SCAN_DIRS),
            "nodes_scanned": len(after) - len(unreadable),
            "unreadable_nodes": unreadable,
            "flagged": flagged[:EVIDENCE_CAP],
            "flagged_count": len(flagged),
            "new_processes": new_processes[:EVIDENCE_CAP],
            "changes": changes,
        }


# ---------------------------------------------------------------------------
# Live wiring
# ---------------------------------------------------------------------------


def session_host_api(session_id: str) -> tuple[Any, dict[str, Any]]:
    """The host API and session metadata the agent's MCP tools use."""
    from nika.runtime.factory import resolve_backend
    from nika.service.lab.host_api import create_host_api
    from nika.utils.session_store import SessionStore

    meta = SessionStore().get_session(session_id)
    lab_name = meta.get("lab_name") or (meta.get("scenario_params") or {}).get(
        "lab_name"
    )
    api = create_host_api(
        lab_name=lab_name, backend=resolve_backend(meta), session_meta=meta
    )
    return api, meta


def _machines(net_env: Any, runtime: Any) -> dict[str, Any]:
    machines = dict(getattr(net_env, "machine_identities", None) or {})
    if machines:
        return machines
    return {name: None for name in runtime.list_nodes()}


def run_agent_view(
    session_id: str, net_env: Any, scenario: str, topo_size: str
) -> dict[str, Any]:
    """Run the healthy agent-view probes on a deployed session."""
    from nika.net_env.net_env_pool import get_probe_path
    from nika.net_env.probe_intent import ProbeIntent
    from nika.service.pingmesh import engine as pingmesh_engine
    from nika.service.pingmesh.endpoints import discover_endpoints
    from nika.validation.symptom.probe import run_probe_snapshot

    api, meta = session_host_api(session_id)
    runtime = net_env._build_runtime()
    intent = ProbeIntent.from_metadata(meta.get("metadata"))
    isolated = intent.isolation_matcher() if intent else (lambda _a, _b: False)

    def pingmesh() -> dict[str, Any]:
        return asyncio.run(pingmesh_engine.run_pingmesh_snapshot(api, intent=intent))

    bgp_mode = getattr(net_env, "bgp_mode", None)
    path = get_probe_path(
        scenario,
        topo_size=topo_size or "s",
        deploy_overrides={"bgp_mode": bgp_mode} if bgp_mode else None,
    )

    def path_probe() -> dict[str, Any]:
        out: dict[str, Any] = {}
        if path is None:
            return out
        if path.dst_ip:
            out["ping_ok"] = run_probe_snapshot(runtime, "path_ping", path).ping_ok
        if path.http_url:
            out["http_ok"] = run_probe_snapshot(runtime, "path_http", path).http_ok
        out["path"] = {"src": path.src_host, "dst": path.dst_ip, "url": path.http_url}
        return out

    probe = AgentViewProbe(
        lambda node, command, timeout: runtime.exec(node, command, timeout=timeout),
        machines=_machines(net_env, runtime),
        endpoints=discover_endpoints(api),
        backend=runtime.backend,
        isolated=isolated,
        zone_files=_lab_zone_files(net_env),
        pingmesh=pingmesh,
        path_probe=path_probe if path is not None else None,
    )
    result = probe.run()
    result["probe_intent"] = intent.model_dump(mode="json") if intent else None
    return result


def leak_scan_for(net_env: Any, fault: str) -> LeakScan:
    """A leak scanner over every node of the session, plus P4Runtime tables."""
    runtime = net_env._build_runtime()
    machines = _machines(net_env, runtime)
    p4_read = None
    if "fabric_mgr" in machines and runtime.backend == "kathara":
        from nika.service.kathara import KatharaBMv2API

        bmv2 = KatharaBMv2API(lab_name=runtime.lab_name)

        def p4_read() -> str:
            return bmv2.p4rt_exec("read")

    return LeakScan(
        lambda node, command, timeout: runtime.exec(node, command, timeout=timeout),
        machines=machines,
        fault=fault,
        p4_read=p4_read,
    )
