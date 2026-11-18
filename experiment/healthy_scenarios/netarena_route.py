"""Measure healthy NetArena Route networks inside a disposable container.

Use the upstream initializer, healthy ping observation, and network stop.
The container must own its Mininet and Open vSwitch resources.
"""

from __future__ import annotations

import argparse
import functools
import json
import shlex
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

UPSTREAM = "525ca6a80fce5a8faef508b70137fbd0513a85cb"


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--switches", type=int, required=True)
    parser.add_argument("--hosts", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not Path("/.dockerenv").exists():
        parser.error("Use a disposable container that owns its Mininet resources")
    commit = subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={args.checkout.resolve()}",
            "-C",
            str(args.checkout),
            "rev-parse",
            "HEAD",
        ],
        text=True,
    ).strip()
    if commit != UPSTREAM:
        parser.error(f"Expected upstream {UPSTREAM}, found {commit}")
    sys.path.insert(0, str(args.checkout.resolve() / "app-route"))
    from topology import initialize_network
    from parallel_ping import parallelPing
    from mininet.net import Mininet
    from mininet.node import Node
    from experiment.healthy_scenarios.sample import SCRIPT, sample

    args.out.mkdir(parents=True, exist_ok=True)
    active = None

    def trace(owner, method, phase):
        original = getattr(owner, method)

        @functools.wraps(original)
        def measured(instance, *values, **keywords):
            started, epoch = time.monotonic_ns(), time.time_ns()
            try:
                return original(instance, *values, **keywords)
            finally:
                if active is not None:
                    active["phases"].append(
                        {
                            "phase": phase,
                            "node": getattr(instance, "name", None),
                            "start_epoch_ns": epoch,
                            "duration_ns": time.monotonic_ns() - started,
                        }
                    )

        setattr(owner, method, measured)

    trace(Mininet, "build", "mininet_build")
    trace(Mininet, "start", "mininet_start")
    trace(Mininet, "configHosts", "host_configuration")
    trace(Node, "setIP", "address_configuration")
    for index in range(1, 4):
        path = args.out / f"run_{index:02d}.json"
        if path.exists():
            raise FileExistsError(path)
        record = {
            "upstream": commit,
            "healthy_only": True,
            "switches": args.switches,
            "hosts_per_subnet": args.hosts,
            "phases": [],
        }
        active = record
        net = None

        def timed(phase, function):
            started, epoch = time.monotonic_ns(), time.time_ns()
            try:
                return function()
            finally:
                record["phases"].append(
                    {
                        "phase": phase,
                        "start_epoch_ns": epoch,
                        "duration_ns": time.monotonic_ns() - started,
                    }
                )

        try:
            _subnets, _topology, net, _router = timed(
                "deployment",
                lambda: initialize_network(args.hosts, args.switches, index),
            )
            _observations, loss = timed(
                "health_verification", lambda: parallelPing(net, timeout=0.1)
            )
            record.update(
                loss_percent=loss, status="complete" if loss == 0 else "unhealthy"
            )
            ready = time.monotonic()

            def export():
                nodes = {host.name: {"raw": host.cmd(SCRIPT)} for host in net.hosts}
                for switch in net.switches:
                    bridge = shlex.quote(switch.name)
                    nodes[switch.name] = {
                        "raw": "@@@ ovs_flows\n"
                        + switch.cmd(f"ovs-ofctl dump-flows {bridge}")
                        + "\n@@@ ovs_ports\n"
                        + switch.cmd(f"ovs-ofctl dump-ports {bridge}")
                    }
                return nodes

            sample(
                args.out / f"run_{index:02d}",
                ready,
                export,
            )
        except BaseException:
            record.update(status="failed", error=traceback.format_exc())
            raise
        finally:
            try:
                if net is not None:
                    timed("teardown", net.stop)
            except BaseException:
                record["cleanup_error"] = traceback.format_exc()
                raise
            finally:
                path.write_text(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
