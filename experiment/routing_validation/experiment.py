"""Complete and check authored routing requirements on an existing NIKA scenario design."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nika.workflows.validation.direct_bgp import build_direct_bgp_experiment
from nika.workflows.validation.routing import validate_routing_design
from nika.workflows.validation.routing_spec import RoutingExperiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specification", type=Path, nargs="?")
    parser.add_argument(
        "--direct-bgp",
        metavar="SCENARIO",
        help="Derive the specification from a two-router vendor eBGP scenario.",
    )
    parser.add_argument("--routing-model", help="Routing model for --direct-bgp.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--image", default="nika/netcomplete:experiment")
    args = parser.parse_args()
    if bool(args.specification) == bool(args.direct_bgp):
        parser.error("Select a specification file or --direct-bgp")
    if args.direct_bgp:
        if not args.routing_model:
            parser.error("--direct-bgp requires --routing-model")
        spec = build_direct_bgp_experiment(
            args.direct_bgp, routing_model=args.routing_model
        )
    else:
        spec = RoutingExperiment.model_validate_json(args.specification.read_text())
    result = validate_routing_design(
        spec, args.output, runtime=args.runtime, image=args.image
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
