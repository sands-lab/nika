"""Check that native renderers and netlab produce routing-equivalent candidates.

Both candidates come from the same environment design: one environment instance
keeps NIKA's deployed configuration, a second one receives the netlab output
through the same binding used by ``--config-generator netlab``. Each candidate
is exported with the platform's existing snapshot builder and compared through
:mod:`nika.validation.consistency`.
"""

from __future__ import annotations

import json
from functools import partial
from uuid import uuid4
from pathlib import Path
from typing import Any

from nika.net_env.contract import ValidationContract
from nika.validation.consistency import (
    BatfishFactCollector,
    ConsistencyReport,
    diff_facts,
    fact_counts,
    probe_routes,
)


def consistency_snapshot_builder(env, routing_model: str | None = None):
    """Select the platform's Batfish export, or the documented projection."""
    from nika.net_env.isp.igp.plan import IspPlan

    if routing_model is None:
        if getattr(env, "device_profile", None) == "sonic":
            routing_model = "sonic"
        elif isinstance(getattr(env, "plan", None), IspPlan):
            profile = env.device_profile
            routing_model = (
                "srl-isp"
                if profile == "nokia_srlinux"
                else f"{profile}-isp"
                if profile in {"iosxr", "routeros"}
                else "frr-isis"
                if env.plan.igp == "isis"
                else "frr"
            )
        elif hasattr(env, "BGP_ROUTERS"):
            (routing_model,) = sorted(
                m for m in env.ROUTING_MODELS if m in {"iosxr-bgp", "routeros-bgp"}
            )
        elif env.backend == "containerlab":
            routing_model = "srl-bgp"
        elif "import_preferences" in env.ROUTING_PARAMETER_KINDS:
            routing_model = "frr-overlay"
        else:
            routing_model = "frr"
    if routing_model == "srl-isp":
        from nika.validation.batfish.srl_isp import build_srl_isp_snapshot

        return routing_model, build_srl_isp_snapshot
    if routing_model == "sonic":
        from nika.validation.batfish.sonic import build_sonic_snapshot

        return routing_model, build_sonic_snapshot
    if routing_model == "srl-bgp":
        from nika.validation.batfish.srl import build_srl_snapshot

        return routing_model, build_srl_snapshot
    if routing_model in {"iosxr-isp", "routeros-isp", "iosxr-bgp", "routeros-bgp"}:
        from nika.validation.batfish.vendor_bgp import build_vendor_snapshot

        return routing_model, partial(
            build_vendor_snapshot, routing_model=routing_model
        )
    if routing_model == "frr-isis":
        from nika.validation.batfish.isis import build_isis_snapshot

        return routing_model, build_isis_snapshot
    if routing_model == "frr-overlay":
        from nika.validation.batfish.overlay import build_overlay_snapshot

        return routing_model, build_overlay_snapshot
    if routing_model == "frr":
        from nika.validation.batfish.kathara import build_kathara_snapshot

        return routing_model, partial(build_kathara_snapshot, routing_model="frr")
    raise ValueError(f"No consistency projection for routing model {routing_model!r}")


def declared_routing_inputs(env) -> tuple[set[str], tuple[str, ...]]:
    """Prefixes and signaling communities named by the environment design."""
    prefixes: set[str] = set()
    communities: tuple[str, ...] = ()
    plan = getattr(env, "bgp_plan", None)
    if plan is not None:
        from nika.net_env.isp.bgp.policy import business_prefixes

        prefixes.update(business_prefixes(o.prefix for o in plan.originated))
        prefixes.update(plan.inventory.get("leak_prefixes") or ())
        if target := plan.inventory.get("target_prefix"):
            prefixes.add(target)
        for node in plan.nodes:
            prefixes.update(node.export_deny_prefixes)
            for _, terms in node.import_policies:
                prefixes.update(term.prefix for term in terms)
        if community := plan.inventory.get("blackhole_community"):
            communities = (str(community),)
    elif hasattr(env, "bgp_origin_prefixes") and getattr(env, "lab", None):
        prefixes.update(env.bgp_origin_prefixes())
    return prefixes, communities


def _contract(scenario: str, name: str) -> ValidationContract:
    # Snapshot builders require a contract; the comparison declares no intents.
    return ValidationContract(
        contract_id=f"consistency-{scenario}-{name}",
        scenario=scenario,
        design_source={"kind": "generator-consistency"},
        intents=(),
    )


def compare_config_generators(
    scenario: str,
    output: Path,
    *,
    topo_size: str | None = None,
    deployment: dict[str, Any] | None = None,
    parameters=None,
    routing_model: str | None = None,
    batfish_host: str = "127.0.0.1",
    batfish_port: int = 9996,
) -> ConsistencyReport:
    """Render ``scenario`` natively and with netlab; compare Batfish routing facts.

    ``deployment`` takes the usual environment options (``backend``, ``igp``,
    ``bgp_mode``, ``device_profile``, ``rpki``). ``parameters`` optionally
    binds completed routing parameters to both candidates. The report and both
    snapshots are written under ``output``. Batfish must already be running.
    """
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.net_env.synthesis.configuration import apply_routing_configs
    from nika.net_env.synthesis.netlab import NETLAB_VERSION
    from nika.workflows.validation.netlab import check_netlab_scope, generate_configs

    deployment = {k: v for k, v in (deployment or {}).items() if v is not None}
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").unlink(missing_ok=True)
    kwargs = dict(deployment)
    if topo_size is not None:
        kwargs["topo_size"] = topo_size
    if parameters is not None:
        kwargs["routing_parameters"] = parameters.model_dump(mode="json")

    native_env = get_net_env_instance(scenario, **kwargs)
    routing_model, builder = consistency_snapshot_builder(native_env, routing_model)
    if getattr(native_env, "native_routers", None):
        from nika.net_env.native_routers import require_native_validation

        # Only these projections export native routers next to FRR routers.
        if routing_model not in {"frr", "frr-overlay"}:
            raise ValueError(
                f"Generator comparison of mixed-vendor deployments does not cover {routing_model}"
            )
        require_native_validation(native_env)
    candidate_env = get_net_env_instance(scenario, **kwargs)
    check_netlab_scope(None, candidate_env)
    generated = generate_configs(None, candidate_env, parameters, output / "netlab")
    apply_routing_configs(candidate_env, generated)

    snapshots = {
        name: builder(env, _contract(scenario, name), output / name)
        for name, env in (("native", native_env), ("netlab", candidate_env))
    }
    collector = BatfishFactCollector(
        f"nika_consistency_{uuid4().hex[:12]}", host=batfish_host, port=batfish_port
    )
    try:
        loaded = {
            name: collector.load(name, snapshot) for name, snapshot in snapshots.items()
        }
        ribs = []
        for name in snapshots:
            collector.use(name)
            ribs.append(collector.bgp_rib())
        prefixes, communities = declared_routing_inputs(native_env)
        probes = probe_routes(ribs, prefixes, communities)
        facts = {}
        for name in snapshots:
            collector.use(name)
            facts[name] = collector.collect(probes)
            counts = fact_counts(facts[name])
            if not counts["interface"] or not counts["rib"]:
                # Two empty projections would otherwise compare as consistent.
                raise RuntimeError(f"The {name} candidate yielded no routing facts")
            (output / f"facts-{name}.json").write_text(
                json.dumps(
                    [
                        {"kind": k, "node": n, "key": d, "value": v}
                        for (k, n, d), v in sorted(facts[name].items())
                    ],
                    indent=2,
                )
                + "\n"
            )
    finally:
        collector.close()
    report = ConsistencyReport(
        scenario=scenario,
        deployment={**deployment, **({"topo_size": topo_size} if topo_size else {})},
        candidate=f"netlab {NETLAB_VERSION}",
        routing_model=routing_model,
        scope={
            name: {
                **{
                    k: v
                    for k, v in snapshot.metadata.items()
                    if k in {"scope", "native_config_analysis", "routing_model"}
                },
                "batfish": loaded[name]["components"],
                "parse_status": loaded[name]["parse_status"],
            }
            for name, snapshot in snapshots.items()
        }
        | {"probe_routes": len(probes)},
        fact_counts={name: fact_counts(f) for name, f in facts.items()},
        differences=diff_facts(facts["native"], facts["netlab"]),
    )
    report.write(output / "report.json")
    return report
