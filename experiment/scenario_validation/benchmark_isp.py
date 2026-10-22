"""Prepare or validate ISP profiles from benchmarks or registered defaults."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nika.workflows.validation.benchmark_routing import (
    benchmark_isp_profiles,
    build_benchmark_isp_experiment,
    isp_catalog_profiles,
)
from nika.workflows.validation.scenario import validate_scenario_design


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", type=Path, nargs="*")
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--config-generator", choices=["netlab"], default=None)
    parser.add_argument("--scenario", action="append", default=[])
    args = parser.parse_args()
    if args.runtime and not args.run:
        parser.error("--runtime requires --run")
    if args.catalog == bool(args.manifests):
        parser.error("Select benchmark manifests or --catalog")
    profiles = (
        isp_catalog_profiles()
        if args.catalog
        else benchmark_isp_profiles(args.manifests)
    )
    if args.scenario:
        unknown = set(args.scenario) - {p["scenario"] for p in profiles.values()}
        if unknown:
            parser.error(
                "Scenarios absent from the selected inventory: "
                + ", ".join(sorted(unknown))
            )
        profiles = {k: p for k, p in profiles.items() if p["scenario"] in args.scenario}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "coverage.json").write_text(json.dumps(profiles, indent=2) + "\n")
    results = {}
    for key, profile in profiles.items():
        if not profile["supported"]:
            results[key] = {
                "status": "unsupported",
                "limitations": profile["limitations"],
            }
            continue
        spec = build_benchmark_isp_experiment(profile)
        if args.config_generator == "netlab":
            from nika.problems.rca.inventory import load_offline_net_env
            from nika.workflows.validation.netlab import check_netlab_scope

            env = load_offline_net_env(
                spec.scenario, **spec.deployment.model_dump(exclude_none=True)
            )
            try:
                check_netlab_scope(spec, env)
            except ValueError as exc:
                profile["supported"] = False
                profile["limitations"] = [*profile["limitations"], str(exc)]
                results[key] = {
                    "status": "unsupported",
                    "limitations": profile["limitations"],
                }
                (args.output / "coverage.json").write_text(
                    json.dumps(profiles, indent=2) + "\n"
                )
                (args.output / "results.json").write_text(
                    json.dumps(results, indent=2) + "\n"
                )
                continue
        root = args.output / key
        root.mkdir(parents=True, exist_ok=True)
        (root / "specification.json").write_text(spec.model_dump_json(indent=2) + "\n")
        results[key] = {"status": "prepared"}
        if args.run:
            results[key] = {
                "status": "passed",
                "summary": validate_scenario_design(
                    spec,
                    root,
                    runtime=args.runtime,
                    config_generator=args.config_generator,
                ),
            }
        (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
