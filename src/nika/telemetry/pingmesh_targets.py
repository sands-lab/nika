"""Topology-grouped rotating targets, also executed on the telemetry collector."""

from __future__ import annotations

import fcntl
import itertools
import json
import math
import os
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

INTERVAL = 15
ROTATE_SECONDS = 30
# At most 128 active scrapes per emulator host, including service probes (8.54 probes/s).
TARGET_BUDGET = 128
TARGET_FILE = "/etc/telemetry/pingmesh_targets.json"
PLAN_FILE = "/etc/telemetry/pingmesh_plan.json"
BUDGET_DIR = "/var/run/probe-budget"
STATUS_PORT = 9103
TCP_PORT = 9116
PROBER_PORT = 9115


def all_targets(endpoints: dict, partitions: list) -> list[dict]:
    indexes = [
        {n: i for i, group in enumerate(groups) for n in group} for groups in partitions
    ]
    buckets: dict[tuple[str, str], list[dict]] = {}
    for source, target in itertools.permutations(sorted(endpoints), 2):
        src, dst = endpoints[source], endpoints[target]
        isolated = any(
            source in idx and target in idx and idx[source] != idx[target]
            for idx in indexes
        )
        labels = {
            "source": source,
            "target": target,
            "source_group": src["group"],
            "target_group": dst["group"],
            "expected": "isolated" if isolated else "reachable",
        }
        probes = [
            ("tcp_connect", f"{dst['address']}:{TCP_PORT}"),
            ("icmp", dst["address"]),
        ]
        if dst.get("service") == "web" or dst.get("http"):
            probes.append(("http", f"http://{dst['address']}/"))
        if dst.get("service") == "dns":
            probes.append(("dns", f"{dst['address']}:53"))
        bucket = buckets.setdefault((src["group"], dst["group"]), [])
        for module, address in probes:
            bucket.append(
                {
                    "targets": [f"{src['management']}:{PROBER_PORT}"],
                    "labels": {
                        **labels,
                        "__param_target": address,
                        "__param_module": module,
                        "module": module,
                    },
                }
            )
    # Interleave attachment groups, then rotate through every member of each group.
    return [
        row
        for batch in itertools.zip_longest(*(buckets[k] for k in sorted(buckets)))
        for row in batch
        if row is not None
    ]


def target_batch(
    targets: list[dict], turn: int, budget: int = TARGET_BUDGET
) -> list[dict]:
    if budget <= 0:
        return []
    if len(targets) <= budget:
        return targets
    pages = math.ceil(len(targets) / budget)
    start = (turn % pages) * budget
    return targets[start : start + budget]


def write_targets(targets: list[dict]) -> None:
    tmp = TARGET_FILE + ".tmp"
    Path(tmp).write_text(json.dumps(targets))
    os.replace(tmp, TARGET_FILE)


def host_budget(registration: Path) -> int:
    """Divide active scrape slots across collectors sharing the emulator host."""
    active = [registration.name]
    for candidate in registration.parent.glob("*.lease"):
        if candidate == registration:
            continue
        with candidate.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active.append(candidate.name)
            else:
                candidate.unlink(missing_ok=True)
    active.sort()
    count = len(active)
    rank = (
        active.index(registration.name) + int(time.time() // ROTATE_SECONDS)
    ) % count
    return TARGET_BUDGET // count + int(rank < TARGET_BUDGET % count)


def main() -> None:
    plan = json.loads(Path(PLAN_FILE).read_text())
    targets = all_targets(plan["endpoints"], plan.get("isolation_partitions", []))
    registration = Path(BUDGET_DIR) / (uuid.uuid4().hex + ".lease")
    pending = registration.with_suffix(".pending")
    lease = pending.open("w")
    fcntl.flock(lease, fcntl.LOCK_EX)
    pending.rename(registration)
    status = {"active": 0, "budget": 0, "targets": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/targets":
                body = json.dumps(status["targets"]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            budget = status["budget"]
            window = math.ceil(len(targets) / budget) * ROTATE_SECONDS if budget else 0
            lines = [
                f"pingmesh_candidate_targets {len(targets)}",
                f"pingmesh_active_targets {status['active']}",
                f"pingmesh_coverage_window_seconds {window}",
                f"pingmesh_probe_budget_per_second {budget / INTERVAL}",
            ]
            lines.extend(
                "pingmesh_endpoint_supported{node=%s} %d"
                % (json.dumps(node), int(node in plan["endpoints"]))
                for node in plan.get("declared_endpoints", plan["endpoints"])
            )
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    threading.Thread(
        target=HTTPServer(("127.0.0.1", STATUS_PORT), Handler).serve_forever,
        daemon=True,
    ).start()
    # Let existing collectors observe the new lease before adding probe traffic.
    time.sleep(5)
    turn = 0
    while True:
        budget = host_budget(registration)
        batch = target_batch(targets, turn, budget)
        status.update(active=len(batch), budget=budget, targets=batch)
        write_targets(batch)
        turn += 1
        # Rebalance leases between rotations without changing representatives.
        for _ in range(6):
            time.sleep(5)
            updated = host_budget(registration)
            if updated != budget:
                budget = updated
                batch = target_batch(targets, turn - 1, budget)
                status.update(active=len(batch), budget=budget, targets=batch)
                write_targets(batch)


if __name__ == "__main__":
    main()
