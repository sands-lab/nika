"""Reuse verified scenario deployment choices and configurations at session startup."""

import json
from pathlib import Path

from nika.net_env.isp.identity import is_isp_named_special
from nika.net_env.synthesis.parameters import RoutingParameters
from nika.workflows.validation.scenario_spec import (
    ENV_ROUTING_MODELS,
    ScenarioExperiment,
)


def scenario_start_kwargs(
    spec: ScenarioExperiment,
    parameters: RoutingParameters | None,
    *,
    configs: dict[str, str] | None = None,
) -> dict:
    """Bind the same deployment choices during verification and later execution."""
    options = spec.deployment.model_dump(exclude_none=True)
    if spec.routing_model in ENV_ROUTING_MODELS:
        options["routing_model"] = spec.routing_model
    if is_isp_named_special(spec.scenario):
        # These constructors retain their registered protocol stack.
        for key in ("igp", "bgp_mode", "rpki", "device_profile"):
            options.pop(key, None)
    return {
        **({"routing_configs": configs} if configs is not None else {}),
        "scenario": spec.scenario,
        "topo_size": spec.topo_size,
        **options,
        "routing_parameters": parameters.model_dump(mode="json")
        if parameters
        else None,
        "static_validation": False,
    }


def load_scenario_build(directory: Path) -> dict:
    """Read a successful construction's startup arguments without running tools.

    The saved summary retains whether acceptance was static-only or also covered
    native execution. This loader does not add verification evidence.
    """
    summary = json.loads((directory / "summary.json").read_text())
    if summary.get("status") != "passed":
        raise ValueError("Scenario build requires a successful validation summary")
    spec = ScenarioExperiment.model_validate_json(
        (directory / "original-spec.json").read_text()
    )
    parameters = (
        RoutingParameters.model_validate_json(
            (directory / "routing-parameters.json").read_text()
        )
        if spec.completion is not None
        else None
    )
    configs = None
    if summary.get("configuration", {}).get("generator") == "netlab":
        configs = {
            p.stem: p.read_text()
            for p in (directory / "candidate-configs").glob("*")
            if p.suffix in {".conf", ".yaml"}
        }
    if configs == {}:
        raise FileNotFoundError(
            "Saved netlab construction is missing candidate configurations"
        )
    return scenario_start_kwargs(spec, parameters, configs=configs)
