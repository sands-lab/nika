"""Collector-side flow counters. This module also runs as a standalone script."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FLOW_ROOT = Path("/var/lib/flows")
INVENTORY = Path("/var/lib/flow_inventory.json")
PORT = 9102


def load_inventory() -> dict:
    return json.loads(INVENTORY.read_text())


def aggregate_file(path: Path) -> list[list[str]]:
    raw = subprocess.check_output(
        [
            "nfdump",
            "-C",
            "none",
            "-r",
            str(path),
            "-q",
            "-N",
            "-A",
            "router,proto,inif",
            "-o",
            "fmt:%ra|%pr|%in|%fl|%pkt|%byt|%te",
        ],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=20,
    )
    return [
        parts
        for line in raw.splitlines()
        if len(parts := [p.strip() for p in line.split("|")]) == 7
    ]


class FlowCounters:
    def __init__(self, inventory: dict) -> None:
        self.inventory = inventory
        self.names = {v["address"]: n for n, v in inventory.items() if v.get("address")}
        self.counters: dict[tuple[str, str, str], list[int]] = defaultdict(
            lambda: [0, 0, 0]
        )
        self.updated: dict[str, float] = {}
        self.newest: dict[str, str] = {}
        self.seen: set[str] = set()
        self.lock = threading.Lock()

    def ingest(self, path: Path, rows: list[list[str]]) -> None:
        with self.lock:
            if str(path) in self.seen:
                return
            for address, proto, index, flows, packets, size, last in rows:
                node = self.names.get(address)
                if node is None:
                    continue
                iface = (
                    self.inventory[node]
                    .get("interfaces", {})
                    .get(index, {})
                    .get("name", index)
                )
                values = self.counters[node, proto, iface]
                for i, value in enumerate((flows, packets, size)):
                    values[i] += int(value)
                self.updated[node] = path.stat().st_mtime
                self.newest[node] = max(last, self.newest.get(node, ""))
            self.seen.add(str(path))

    def render(self) -> str:
        lines = []
        with self.lock:
            for i, kind in enumerate(("flows", "packets", "bytes")):
                metric = f"netflow_{kind}_total"
                lines += [
                    f"# HELP {metric} Exported {kind}; summing nodes counts traffic once per hop.",
                    f"# TYPE {metric} counter",
                ]
                for (node, proto, iface), values in sorted(self.counters.items()):
                    labels = f"node={json.dumps(node)},proto={json.dumps(proto)},in_if={json.dumps(iface)}"
                    lines.append(f"{metric}{{{labels}}} {values[i]}")
            lines += [
                "# HELP netflow_last_file_timestamp_seconds Capture file modification time for this exporter.",
                "# TYPE netflow_last_file_timestamp_seconds gauge",
            ]
            for node, stamp in sorted(self.updated.items()):
                lines.append(
                    f"netflow_last_file_timestamp_seconds{{node={json.dumps(node)}}} {stamp}"
                )
        return "\n".join(lines) + "\n"

    def coverage(self) -> dict:
        with self.lock:
            return {
                node: {
                    **info,
                    "newest_record": self.newest.get(node),
                    "last_update": self.updated.get(node),
                }
                for node, info in self.inventory.items()
            }


def main() -> None:
    counters = FlowCounters(load_inventory())

    def poll() -> None:
        while True:
            for path in sorted(FLOW_ROOT.rglob("nfcapd.*")):
                if (
                    not path.is_file()
                    or not path.name.removeprefix("nfcapd.").isdigit()
                    or str(path) in counters.seen
                ):
                    continue
                try:
                    counters.ingest(path, aggregate_file(path))
                except (OSError, ValueError, subprocess.SubprocessError):
                    logging.exception("Cannot import capture %s", path)
            time.sleep(2)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                body = counters.render().encode()
            elif self.path == "/coverage":
                body = json.dumps(counters.coverage()).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Type",
                "text/plain; version=0.0.4"
                if self.path == "/metrics"
                else "application/json",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    threading.Thread(target=poll, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
