"""Complete and check authored routing requirements on an existing NIKA scenario design."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nika.workflows.validation.routing import validate_routing_design
from nika.workflows.validation.routing_spec import RoutingExperiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specification", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--config-generator", choices=["netlab"], default=None)
    parser.add_argument("--image", default="nika/netcomplete:experiment")
    args = parser.parse_args()
    result = validate_routing_design(
        RoutingExperiment.model_validate_json(args.specification.read_text()),
        args.output,
        runtime=args.runtime,
        image=args.image,
        config_generator=args.config_generator,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
