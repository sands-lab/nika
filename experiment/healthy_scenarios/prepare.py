"""Build a healthy snapshot before the three timed restores."""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["firecracker", "incus"], required=True)
    parser.add_argument("--params", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--incus-base")
    args = parser.parse_args()
    if args.mode == "incus" and not args.incus_base:
        parser.error("Incus preparation requires an instance/snapshot")
    from experiment.healthy_scenarios.phases import install
    from experiment.healthy_scenarios.guest_probe import install as install_guests

    from experiment.healthy_scenarios.cpu import alignment

    with alignment(args.mode):
        install()
        install_guests()
        params = json.loads(args.params.read_text())
        started = time.monotonic()
        if args.mode == "firecracker":
            from nika.firecracker.image import ensure_image, META

            image = ensure_image(params)
            identity = json.loads((image.path / META).read_text())
        else:
            from experiment.incus.config import configure
            from experiment.incus.golden import ensure_golden
            from experiment.incus.incus import incus

            configure(args.incus_base)
            golden = ensure_golden(params)
            identity = json.loads(
                incus("query", f"/1.0/instances/{golden.name}/snapshots/golden")
            )
    args.receipt.write_text(
        json.dumps(
            {
                "mode": args.mode,
                "params": params,
                "prepare_sec": time.monotonic() - started,
                "identity": identity,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
