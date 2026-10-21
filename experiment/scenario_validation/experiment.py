"""Construct and verify a NIKA scenario against its original behavioral requirements."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nika.workflows.validation.direct_bgp import build_direct_bgp_experiment
from nika.workflows.validation.scenario import validate_scenario_design
from nika.workflows.validation.scenario_spec import ScenarioExperiment


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
    parser.add_argument("--config-generator", choices=["netlab"], default=None)
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
        spec = ScenarioExperiment.model_validate_json(args.specification.read_text())
    result = validate_scenario_design(
        spec,
        args.output,
        runtime=args.runtime,
        image=args.image,
        config_generator=args.config_generator,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
