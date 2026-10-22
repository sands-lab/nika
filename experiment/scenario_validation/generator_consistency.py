"""Compare NIKA's native configuration with netlab output for one environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nika.workflows.validation.consistency import compare_config_generators


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--topo-size", choices=["s", "m", "l"])
    parser.add_argument("--backend", choices=["kathara", "containerlab"])
    parser.add_argument("--igp", choices=["ospf", "isis"])
    parser.add_argument("--bgp-mode", choices=["none", "ibgp_rr", "ebgp"])
    parser.add_argument("--device-profile")
    parser.add_argument("--rpki", action="store_true", default=None)
    parser.add_argument("--routing-model", help="Override the selected projection")
    parser.add_argument("--batfish-host", default="127.0.0.1")
    parser.add_argument("--batfish-port", type=int, default=9996)
    args = parser.parse_args()
    report = compare_config_generators(
        args.scenario,
        args.output,
        topo_size=args.topo_size,
        deployment={
            "backend": args.backend,
            "igp": args.igp,
            "bgp_mode": args.bgp_mode,
            "device_profile": args.device_profile,
            "rpki": args.rpki,
        },
        routing_model=args.routing_model,
        batfish_host=args.batfish_host,
        batfish_port=args.batfish_port,
    )
    print(
        json.dumps(
            {
                "status": "consistent" if report.consistent else "different",
                "differences": report.summary(),
                "report": str(args.output.resolve() / "report.json"),
            },
            indent=2,
        )
    )
    raise SystemExit(0 if report.consistent else 1)


if __name__ == "__main__":
    main()
