"""Use the shared specification/analysis/execution workflow for packet fabrics."""

import json
import shlex
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from nika.net_env import p4_dc_fabric, p4_dc_gateway

from nika.net_env.contract import (
    NetworkEntity,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
)
from nika.net_env.isp.kathara.verify import verify_isp_contract
from nika.net_env.p4_dc_fabric.topology_model import ClosFabricModel as P4Clos
from nika.net_env.p4_dc_gateway.topology_model import GatewayFabricModel
from nika.net_env.sdn_l3_clos.topology_model import ClosFabricModel as OpenFlowClos
from nika.problems.rca.inventory import load_offline_net_env
from nika.problems.support.polling import wait_until
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.validation.dispatcher import VerifierDispatcher
from nika.validation.netkat.adapters import (
    endpoints_from_model,
    openflow_network,
    p4runtime_network,
)
from nika.validation.netkat.p4 import compile_forwarding_slice
from nika.validation.netkat.verifier import NetKatVerifier, write_snapshot
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from nika.workflows.validation.scenario_build import scenario_start_kwargs
from nika.workflows.validation.scenario_spec import ScenarioExperiment, ScenarioIncident


def _frontend(env):
    from nika.net_env.p4_dc_fabric.fabric_manager.intent import build_forwarding_intent
    from nika.net_env.p4_dc_gateway.control import build_gateway_intent
    from nika.net_env.sdn_l3_clos.fabric_manager.forwarding_rules import (
        build_forwarding_rules,
    )

    # Dispatch on the actual topology capability, never the registered scenario name.
    frontends = {
        OpenFlowClos: (build_forwarding_rules, openflow_network, None),
        P4Clos: (
            build_forwarding_intent,
            p4runtime_network,
            Path(p4_dc_fabric.__file__).parent / "fabric.p4",
        ),
        GatewayFabricModel: (
            build_gateway_intent,
            p4runtime_network,
            Path(p4_dc_gateway.__file__).parent / "gateway.p4",
        ),
    }
    if type(getattr(env, "model", None)) not in frontends:
        raise ValueError(
            "No supported packet-forwarding frontend for this topology model"
        )
    return frontends[type(env.model)]


def build_forwarding_experiment(scenario, *, topo_size="s"):
    """Declare endpoint communication independently of generated forwarding rules."""
    env = load_offline_net_env(scenario, topo_size=topo_size)
    _frontend(env)
    endpoints = endpoints_from_model(env.model)
    clients = {
        e.name for e in env.model.endpoints if e.role in {"client", "external_client"}
    }
    services = {
        e.name for e in env.model.endpoints if e.role in {"web", "http_service"}
    }
    if clients | services != endpoints.keys() or not clients or not services:
        raise ValueError(
            "Declare client and service roles before constructing the communication specification"
        )
    intents = []
    for source, src in endpoints.items():
        entity = NetworkEntity(
            kind="endpoint", name=source, address=src.address, node=source
        )
        for destination, dst in endpoints.items():
            if not (
                (source in clients and destination in services)
                or (source in services and destination in clients)
            ):
                continue
            intents.append(
                ValidationIntent(
                    id=f"reach.{source}.{destination}",
                    description="Declared client-service communication in the fabric",
                    property="reachability",
                    expected="reachable",
                    source=entity,
                    destination=NetworkEntity(
                        kind="endpoint",
                        name=destination,
                        address=dst.address,
                        node=destination,
                    ),
                    traffic=TrafficSelector(protocol="icmp", round_trip=True),
                )
            )
        intents.append(
            ValidationIntent(
                id=f"isolation.{source}.unowned",
                description="The fabric must not deliver traffic to the unowned test address",
                property="isolation",
                expected="unreachable",
                source=entity,
                destination=NetworkEntity(
                    kind="endpoint", name="unowned", address="198.18.0.1"
                ),
                traffic=TrafficSelector(protocol="icmp"),
            )
        )
    healthy = ValidationContract(
        contract_id="fabric-healthy",
        scenario=scenario,
        design_source={
            "method": "Authored client-service ICMP communication and unowned-prefix isolation",
            "client_endpoints": sorted(clients),
            "service_endpoints": sorted(services),
            "topology_model": type(env.model).__name__,
            "topo_size": topo_size,
        },
        intents=tuple(sorted(intents, key=lambda i: i.id)),
    )
    target = next(iter(endpoints))
    failed = []
    for intent in healthy.intents:
        if intent.property == "reachability" and target in {
            intent.source.name,
            intent.destination.name,
        }:
            intent = intent.model_copy(
                update={
                    "property": "isolation",
                    "expected": "unreachable",
                    "traffic": TrafficSelector(protocol="icmp"),
                }
            )
        failed.append(intent)
    incident = ScenarioIncident(
        name="endpoint-link-down",
        host_name=target,
        intf_name="eth0",
        requirements=healthy.model_copy(
            update={
                "contract_id": "fabric-endpoint-link-down",
                "intents": tuple(failed),
            }
        ),
    )
    return ScenarioExperiment(
        scenario=scenario,
        routing_model="netkat",
        topo_size=topo_size,
        healthy=healthy,
        incidents=(incident,),
    )


def _accepted(report):
    return (
        bool(report.results)
        and all(r.status == "passed" for r in report.results if r.intent)
        and all(s.status == "passed" for s in report.sanity)
    )


def validate_forwarding_design(spec, output, *, runtime=False):
    """Analyze candidates and exercise original predicates without altering startup defaults."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").unlink(missing_ok=True)
    (output / "original-spec.json").write_text(spec.model_dump_json(indent=2) + "\n")
    if (
        spec.completion
        or spec.behavior
        or spec.counterexamples
        or spec.runtime_counterexamples
        or spec.failure_checks
        or spec.runtime_requirements
    ):
        raise ValueError(
            "This packet workflow supports original packet predicates and endpoint-link incidents"
        )
    deployment = spec.deployment.model_dump(exclude_none=True)
    if deployment and deployment != {"backend": "kathara"}:
        raise ValueError(
            "This packet workflow supports the existing Kathara deployment only"
        )
    if runtime and any(
        i.property == "waypoint"
        for c in (spec.healthy, *(x.requirements for x in spec.incidents))
        for i in c.intents
    ):
        raise ValueError(
            "Native waypoint identity mapping is not supported for packet fabrics yet"
        )
    env = load_offline_net_env(spec.scenario, topo_size=spec.topo_size)
    generator, adapter, program = _frontend(env)
    candidate = generator(env.model)
    (output / "candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")
    network = adapter(env.model, candidate)
    if program:
        compiled = compile_forwarding_slice(program, output / "p4")
        network.metadata["compiled_forwarding_slice"] = compiled
    contracts = {
        "healthy": spec.healthy,
        **{i.name: i.requirements for i in spec.incidents},
    }
    if len(contracts) != len(spec.incidents) + 1 or any(
        c.scenario != spec.scenario or not c.intents for c in contracts.values()
    ):
        raise ValueError(
            "Operating states need unique names and nonempty original requirements for this scenario"
        )
    endpoints = network.endpoints
    for incident in spec.incidents:
        if (
            incident.host_name not in endpoints
            or incident.intf_name != "eth0"
            or incident.runtime_requirements
        ):
            raise ValueError(
                "This forwarding projection supports endpoint eth0 outages"
            )
    verifier = NetKatVerifier(timeout=spec.solver_timeout_sec)
    summary = {
        "scenario": spec.scenario,
        "configuration": {
            "generator": "existing-domain-generator",
            "completion": "not-applicable: forwarding entries have no routing-parameter holes",
        },
        "assumptions": network.assumptions,
        "static": {},
        "runtime": {},
        "counterexamples": {},
    }

    def analyze(name, contract, model, *, require=True):
        snapshot = write_snapshot(model, output / name)
        report = VerifierDispatcher([verifier]).run(
            contract, {"netkat": snapshot}, names=["netkat"]
        )["netkat"]
        report.write(output / f"static-{name}.json")
        if require and not _accepted(report):
            raise RuntimeError(f"Original predicates failed static analysis: {name}")
        summary["static"][name] = report.coverage.model_dump()
        return report

    analyze("healthy", spec.healthy, network)
    for incident in spec.incidents:
        down = network.model_copy(
            update={
                "disabled_links": (
                    (incident.host_name, endpoints[incident.host_name].switch),
                )
            }
        )
        analyze(incident.name, incident.requirements, down)
    # Expectations follow endpoint ownership, before modifying any candidate rule.
    target = next(iter(endpoints))
    ep = endpoints[target]
    expected_ids = {
        i.id
        for i in spec.healthy.intents
        if i.property in {"reachability", "waypoint"}
        and (
            i.destination.name == target
            or (i.traffic.round_trip and i.source.name == target)
        )
    }
    (output / "counterexample-spec.json").write_text(
        json.dumps(
            {
                "endpoint": target,
                "address": ep.address,
                "expected_failed_ids": sorted(expected_ids),
                "change": "P4 LPM withdrawal"
                if program
                else "Higher-priority OpenFlow destination drop",
            },
            indent=2,
        )
        + "\n"
    )
    mutated = deepcopy(candidate)
    if program:
        routes = mutated["switches"][ep.switch]["ipv4_lpm"]
        routes[:] = [r for r in routes if r["prefix"] != f"{ep.address}/32"]
    else:
        mutated["flows"].append(
            {
                "switch": ep.switch,
                "device_id": next(
                    f["device_id"]
                    for f in candidate["flows"]
                    if f["switch"] == ep.switch
                ),
                "table": 0,
                "priority": 55000,
                "match": f"ip,nw_dst={ep.address}",
                "actions": "drop",
            }
        )
    (output / "counterexample-candidate.json").write_text(
        json.dumps(mutated, indent=2) + "\n"
    )
    negative = analyze(
        "missing-endpoint-route",
        spec.healthy,
        adapter(env.model, mutated),
        require=False,
    )
    if (
        any(r.status not in {"passed", "failed"} for r in negative.results)
        or {r.intent for r in negative.results if r.status == "failed"} != expected_ids
        or not expected_ids
    ):
        raise RuntimeError(
            "Route withdrawal must violate exactly the predeclared endpoint predicates"
        )
    summary["counterexamples"]["missing-endpoint-route"] = {
        "status": "rejected",
        "failed_ids": sorted(expected_ids),
    }
    if runtime:
        session_id = f"forwarding-spec-{uuid4().hex[:12]}"
        try:
            start_net_env(
                **scenario_start_kwargs(spec, None),
                session_id=session_id,
                session_dir=str(output / "runtime"),
            )
            session = Session().load_running_session(session_id)
            backend = runtime_for_session(session)

            def observe(name, contract):
                reports = []

                def ready():
                    report = verify_isp_contract(backend, contract=contract, plan=None)
                    report.write(output / f"runtime-{name}.json")
                    reports[:] = [report]
                    return _accepted(report)

                if not wait_until(ready, spec.convergence_timeout_sec):
                    raise RuntimeError(
                        f"Original predicates failed native execution: {name}"
                    )
                summary["runtime"][name] = reports[0].coverage.model_dump()

            observe("healthy", spec.healthy)
            # Apply the same configuration counterexample through the native API.
            if program:
                from nika.net_env.p4_dc_fabric.fabric_manager.apply import run_manager

                result = run_manager(
                    backend,
                    "delete-lpm",
                    "--switch",
                    ep.switch,
                    "--prefix",
                    f"{ep.address}/32",
                )
                if not result.get("ok"):
                    raise RuntimeError("Native LPM withdrawal failed")
            else:
                from nika.net_env.sdn_l3_clos.fabric_manager.apply import (
                    _install_onos_flow_body,
                    _onos_batch_resilient,
                    _onos_json,
                )

                # Controller-owned rules survive ONOS reconciliation during a
                # complete predicate scan, including larger topology sizes.
                body = _install_onos_flow_body(mutated["flows"][-1], {})
                flow_path = f"/onos/v1/flows/{body['deviceId']}"
                _onos_batch_resilient(
                    backend,
                    [("POST", flow_path, body)],
                    operation="counterexample install",
                )
                installed = []

                def counterexample_ready():
                    installed[:] = [
                        f
                        for f in _onos_json(backend, flow_path).get("flows", [])
                        if f.get("priority") == body["priority"]
                        and any(
                            c.get("type") == "IPV4_DST"
                            and c.get("ip") == f"{ep.address}/32"
                            for c in f.get("selector", {}).get("criteria", [])
                        )
                        # ONOS serializes an empty drop treatment as NOACTION.
                        and all(
                            instruction.get("type") == "NOACTION"
                            for instruction in f.get("treatment", {}).get(
                                "instructions", []
                            )
                        )
                        and f.get("state") == "ADDED"
                    ]
                    return len(installed) == 1

                if not wait_until(counterexample_ready, spec.convergence_timeout_sec):
                    raise RuntimeError(
                        "ONOS did not install the declared counterexample"
                    )
                flow_id = installed[0]["id"]
            try:
                observed = verify_isp_contract(
                    backend, contract=spec.healthy, plan=None
                )
                observed.write(output / "runtime-missing-endpoint-route.json")
                if (
                    any(r.status not in {"passed", "failed"} for r in observed.results)
                    or {r.intent for r in observed.results if r.status == "failed"}
                    != expected_ids
                ):
                    raise RuntimeError(
                        "Native route counterexample differs from the static prediction"
                    )
                summary["counterexamples"]["missing-endpoint-route"][
                    "runtime_status"
                ] = "rejected"
            finally:
                if program:
                    if not run_manager(backend, "apply", "--switch", ep.switch).get(
                        "ok"
                    ):
                        raise RuntimeError("Native LPM restoration failed")
                else:
                    _onos_batch_resilient(
                        backend,
                        [("DELETE", f"{flow_path}/{flow_id}", None)],
                        operation="counterexample restore",
                    )
            observe("route-restored", spec.healthy)
            for incident in spec.incidents:
                params = {
                    "host_name": incident.host_name,
                    "intf_name": incident.intf_name,
                }
                # The model removes this endpoint attachment. Disable its own
                # interface only; mutating the switch's packet-I/O interfaces
                # adds implementation behavior outside that fault model.
                backend.set_interface_state(
                    params["host_name"], params["intf_name"], "down"
                )
                try:
                    observe(incident.name, incident.requirements)
                finally:

                    def restore_endpoint():
                        backend.set_interface_state(
                            params["host_name"], params["intf_name"], "up"
                        )
                        # Linux can remove routes and permanent neighbors on
                        # administrative down. Restore the original declarations
                        # on this endpoint, without replaying service startup.
                        startup = env.lab.fs.readtext(f"{params['host_name']}.startup")
                        for line in startup.splitlines():
                            if not line.startswith(
                                ("ip route replace ", "ip neigh replace ")
                            ):
                                continue
                            tokens = shlex.split(line)
                            if tokens[:3] in (
                                ["ip", "route", "replace"],
                                ["ip", "neigh", "replace"],
                            ):
                                backend.exec(params["host_name"], shlex.join(tokens))
                        return (
                            backend.get_interface_operstate(
                                params["host_name"], params["intf_name"]
                            )
                            == "up"
                        )

                    if not wait_until(
                        restore_endpoint,
                        spec.convergence_timeout_sec,
                    ):
                        raise RuntimeError("Native endpoint link restoration failed")
                observe(incident.name + "-recovered", spec.healthy)
        finally:
            try:
                close_session(session_id=session_id)
            except FileNotFoundError:
                pass
    summary.update(
        status="passed",
        validation_scope="static-and-runtime" if runtime else "static-only",
    )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
