"""Construct scenario candidates and verify their original behavioral specifications."""

from __future__ import annotations
import json
import shlex
import time
from pathlib import Path
from ipaddress import IPv4Address, IPv4Interface
from uuid import uuid4
import docker
from docker.errors import NotFound
from nika.net_env.net_env_pool import get_net_env_instance
from nika.net_env.synthesis.completion import complete_routing
from nika.net_env.synthesis.parameters import RoutingParameters
from nika.net_env.synthesis.requirements import (
    resolve_routing_requirements,
)
from nika.net_env.isp.kathara.verify import verify_isp_contract
from nika.problems.link_interface.link import LinkFailure, LinkFailureParams
from nika.problems.support.polling import wait_until
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.logger import system_logger
from nika.validation.batfish.kathara import (
    build_kathara_snapshot,
    startup_addresses,
    startup_static_routes,
)
from nika.validation.batfish.service import (
    BATFISH_CONTAINER_NAME,
    ensure_batfish_service,
)
from nika.validation.batfish.verifier import BatfishVerifier
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session
from nika.workflows.validation.scenario_spec import (
    ENV_ROUTING_MODELS,
    ScenarioExperiment,
)
from nika.workflows.validation.scenario_build import scenario_start_kwargs


# XR commits and configuration replacement can take a minute on a loaded host.
NATIVE_CONFIG_TIMEOUT_SEC = 300


def restore_link_routes(env, backend, host_name: str, intf_name: str):
    """Restore declared kernel routes removed by administrative interface-down.

    Only routes whose original gateway is on the restored link are reapplied.
    No service startup commands or unrelated interface routes are replayed.
    """
    machine = env.lab.machines[host_name]
    link = machine.interfaces[int(intf_name.removeprefix("eth"))].link
    restored = []
    for node in link.machines:
        if not env.lab.fs.exists(f"{node}.startup"):
            continue
        startup = env.lab.fs.readtext(f"{node}.startup")
        interfaces = {
            f"eth{number}"
            for number, interface in env.lab.machines[node].interfaces.items()
            if interface.link.name == link.name
        }
        networks = [
            IPv4Interface(address).network
            for address, interface in startup_addresses(startup)
            if interface in interfaces
        ]
        for prefix, gateway in startup_static_routes(startup):
            if any(IPv4Address(gateway) in network for network in networks):
                backend.exec(
                    node, shlex.join(["ip", "route", "replace", prefix, "via", gateway])
                )
                restored.append({"node": node, "prefix": prefix, "gateway": gateway})
    return restored


def validate_scenario_design(
    spec: ScenarioExperiment,
    output: Path,
    *,
    runtime=False,
    image="nika/netcomplete:experiment",
    parameters: RoutingParameters | None = None,
    batfish_verifier: BatfishVerifier | None = None,
    config_generator: str | None = None,
):
    if spec.routing_model == "netkat":
        from nika.workflows.validation.forwarding import validate_forwarding_design

        if (
            config_generator is not None
            or parameters is not None
            or batfish_verifier is not None
        ):
            raise ValueError(
                "Packet-forwarding experiments use their existing domain configuration generators"
            )
        return validate_forwarding_design(spec, output, runtime=runtime)
    if config_generator not in {None, "netlab"}:
        raise ValueError("Unknown configuration generator")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").unlink(missing_ok=True)
    # Save original behavior before resolving fixed address/session bindings.
    (output / "original-spec.json").write_text(spec.model_dump_json(indent=2) + "\n")
    native_srl = spec.routing_model in {"srl-bgp", "srl-isp"}
    native_vendor = spec.routing_model in {
        "iosxr-bgp",
        "routeros-bgp",
        "iosxr-isp",
        "routeros-isp",
    }
    deploy_options = spec.deployment.model_dump(exclude_none=True)
    if spec.routing_model in ENV_ROUTING_MODELS:
        deploy_options["routing_model"] = spec.routing_model
    env = get_net_env_instance(
        spec.scenario, topo_size=spec.topo_size, **deploy_options
    )
    if getattr(env, "native_routers", None):
        from nika.net_env.native_routers import require_native_validation

        # Only the Ethernet FRR/Linux projections export native routers.
        if spec.routing_model not in {"frr", "linux-static"}:
            raise ValueError(
                "Scenario experiments cover mixed-vendor deployments only with "
                "the frr or linux-static routing model"
            )
        require_native_validation(env)
    if config_generator == "netlab":
        from nika.workflows.validation.netlab import check_netlab_scope

        # Reject unsupported capabilities before running a solver or analyzer.
        check_netlab_scope(spec, env)
    if native_srl:
        from nika.net_env.srl_validation import verify_srl_contract

        if spec.routing_model == "srl-isp":
            from nika.validation.batfish.srl_isp import build_srl_isp_snapshot

            snapshot_builder = build_srl_isp_snapshot
        else:
            if "srl-bgp" not in env.ROUTING_MODELS or spec.completion is None:
                raise ValueError(
                    "SR Linux routing experiments require a supported completion sketch"
                )
            from nika.validation.batfish.srl import build_srl_snapshot

            snapshot_builder = build_srl_snapshot
        runtime_verifier = verify_srl_contract
        if spec.failure_checks and spec.routing_model != "srl-isp":
            raise ValueError(
                "FRR configuration-failure restoration cannot be used on SR Linux"
            )
    elif native_vendor:
        from functools import partial
        from nika.validation.batfish.vendor_bgp import (
            build_vendor_snapshot,
            vendor_configs,
        )
        from nika.net_env.vendor_validation import (
            verify_iosxr_contract,
            verify_routeros_contract,
        )

        if (
            spec.routing_model not in env.ROUTING_MODELS
            or not hasattr(env, "BGP_CONFIG_PATH")
            or spec.completion is None
        ):
            raise ValueError(
                "Native BGP experiments require a supported completion sketch"
            )
        snapshot_builder = partial(
            build_vendor_snapshot, routing_model=spec.routing_model
        )
        runtime_verifier = (
            verify_iosxr_contract
            if spec.routing_model in {"iosxr-bgp", "iosxr-isp"}
            else verify_routeros_contract
        )
        if spec.failure_checks:
            raise ValueError(
                "Native vendor experiments require vendor-specific configuration restoration"
            )
    elif spec.routing_model == "frr-isis":
        from nika.validation.batfish.isis import build_isis_snapshot

        snapshot_builder = build_isis_snapshot
        runtime_verifier = verify_isp_contract
    else:
        snapshot_builder = build_kathara_snapshot
        runtime_verifier = verify_isp_contract
    # Routers an FRR/Linux environment runs on a native platform. Their vendor
    # projection supplies static analysis, native observation and restoration.
    native_routers = dict(getattr(env, "native_routers", {}))
    if native_routers:
        from nika.net_env.native_routers import (
            native_platform,
            verify_native_router_contract,
        )

        def native_validation(node):
            return native_platform(native_routers[node]).validation

    spec = resolve_routing_requirements(spec, env)
    (output / "resolved-spec.json").write_text(spec.model_dump_json(indent=2) + "\n")
    names = (
        ["healthy"]
        + [state.name for state in spec.incidents]
        + [mutation.name for mutation in spec.counterexamples]
        + [mutation.name for mutation in spec.runtime_counterexamples]
        + [failure.name for failure in spec.failure_checks]
    )
    if len(names) != len(set(names)):
        raise ValueError("Operating states and counterexamples require distinct names")
    contracts = {
        "healthy": spec.healthy,
        **{state.name: state.requirements for state in spec.incidents},
    }
    if len(contracts) != 1 + len(spec.incidents):
        raise ValueError(
            "Operating state names must be unique and distinct from healthy"
        )
    if any(
        intent.property == "waypoint"
        for c in contracts.values()
        for intent in c.intents
    ):
        raise ValueError(
            "This Ethernet routing workflow does not yet resolve runtime waypoint identities"
        )
    for c in contracts.values():
        if c.scenario != spec.scenario:
            raise ValueError("Requirements must name the deployed scenario")
    extra_contracts = [
        c
        for c in [spec.runtime_requirements]
        + [i.runtime_requirements for i in spec.incidents]
        if c is not None
    ]
    extra_contracts += [f.requirements for f in spec.failure_checks if f.requirements]
    for contract in list(contracts.values()) + extra_contracts:
        if contract.scenario != spec.scenario or not contract.intents:
            raise ValueError(
                "Every declared requirement set must be nonempty and name the deployed scenario"
            )
    static_ids = {i.id for i in spec.healthy.intents}
    runtime_ids = static_ids | (
        {i.id for i in spec.runtime_requirements.intents}
        if spec.runtime_requirements
        else set()
    )
    if spec.runtime_requirements and static_ids & {
        intent.id for intent in spec.runtime_requirements.intents
    }:
        raise ValueError(
            "Additional runtime requirements must use distinct original IDs"
        )
    for counterexample in spec.counterexamples:
        if not set(counterexample.expected_failed_ids) <= static_ids:
            raise ValueError(
                "Static counterexamples must reference original static requirement IDs"
            )
    for failure in [*spec.failure_checks, *spec.runtime_counterexamples]:
        if not set(failure.expected_failed_ids) <= runtime_ids:
            raise ValueError("Failure checks must reference original requirement IDs")
    (output / "verification-coverage.json").write_text(
        json.dumps(
            {
                "static": sorted(static_ids),
                "runtime": sorted(runtime_ids),
                "runtime_only": sorted(runtime_ids - static_ids),
            },
            indent=2,
        )
        + "\n"
    )
    synthesized_here = parameters is None
    if parameters is not None and spec.completion is None:
        raise ValueError("A completed stage requires a declared completion sketch")
    if spec.completion is not None:
        if parameters is None:
            # Native routers translate the completed FRR sketch, so completion
            # reads the same sketch the FRR deployment would use.
            sketch = (
                get_net_env_instance(
                    spec.scenario,
                    topo_size=spec.topo_size,
                    **{
                        k: v for k, v in deploy_options.items() if k != "device_profile"
                    },
                )
                if native_routers
                else env
            )
            parameters = complete_routing(
                spec, sketch, output / "netcomplete", image=image
            )
        else:
            parameters = RoutingParameters.model_validate(parameters)
        (output / "routing-parameters.json").write_text(
            parameters.model_dump_json(indent=2) + "\n"
        )
        env = get_net_env_instance(
            spec.scenario,
            topo_size=spec.topo_size,
            routing_parameters=parameters.model_dump(mode="json"),
            **deploy_options,
        )
    if native_routers:
        runtime_verifier = verify_native_router_contract(env, runtime_verifier)
    generated_configs = None
    if config_generator == "netlab":
        from nika.net_env.synthesis.configuration import apply_routing_configs
        from nika.workflows.validation.netlab import (
            bind_netlab_controls,
            generate_configs,
        )

        generated_configs = generate_configs(spec, env, parameters, output / "netlab")
        apply_routing_configs(env, generated_configs)
        control_configs = generated_configs
        if native_srl:
            import yaml

            control_configs = {
                node: yaml.safe_dump(document, sort_keys=False)
                for node, document in env.routing_documents.items()
            }
        spec = bind_netlab_controls(
            spec,
            control_configs,
            "nokia_srlinux"
            if native_srl
            else getattr(
                env,
                "device_profile",
                spec.routing_model.split("-")[0] if native_vendor else "frr",
            ),
        )
        (output / "resolved-spec.json").write_text(
            spec.model_dump_json(indent=2) + "\n"
        )
    candidate_dir = output / "candidate-configs"
    candidate_dir.mkdir(exist_ok=True)
    for previous in candidate_dir.iterdir():
        if previous.suffix in {".conf", ".yaml"}:
            previous.unlink()

    def candidate_configs():
        if native_vendor:
            return vendor_configs(env)
        if native_srl:
            import yaml

            return {
                node: yaml.safe_dump(document, sort_keys=False)
                for node, document in env.routing_documents.items()
            }
        return {
            **{
                name: machine.fs.readtext("etc/frr/frr.conf")
                for name, machine in env.lab.machines.items()
                if machine.fs and machine.fs.exists("etc/frr/frr.conf")
            },
            **{name: env.deployment_configs[name] for name in native_routers},
        }

    for name, config in candidate_configs().items():
        (candidate_dir / f"{name}.{'yaml' if native_srl else 'conf'}").write_text(
            config
        )
    summary = {"static": {}, "runtime": {}, "counterexamples": {}}
    if generated_configs is not None:
        from nika.net_env.synthesis.netlab import NETLAB_VERSION

        summary["configuration"] = {"generator": "netlab", "version": NETLAB_VERSION}
        retained = []
        if spec.routing_model == "srl-bgp":
            retained.append("existing IPv6 interface addressing")
        elif native_srl:
            retained.append("IGP ECMP capacity from the completed sketch")
        if spec.routing_model == "routeros-bgp" or (
            getattr(env, "device_profile", None) == "routeros"
            and getattr(env, "bgp_plan", None)
        ):
            retained.append("exact-prefix admission and local-preference filters")
        elif getattr(env, "bgp_plan", None) and any(
            peer.session_type == "ibgp" for peer in env.bgp_plan.sessions
        ):
            retained.append("iBGP attachment of upstream-generated admission policies")
        if "import_preferences" in env.ROUTING_PARAMETER_KINDS:
            retained.append("default-to-business VRF imports")
        if native_routers:
            retained.append(
                "native router static routes, eBGP multipath and open eBGP policies"
            )
        if getattr(env, "rpki", False) or getattr(env, "rtbh", False):
            retained.append("RPKI/RTBH security policy and runtime attachments")
        if retained:
            summary["configuration"]["retained"] = retained
    if parameters is not None:
        summary["synthesis"] = {
            "backend": "netcomplete" if synthesized_here else "provided-stage-output",
            "kind": "composite"
            if isinstance(spec.completion, tuple)
            else spec.completion.kind,
            "parameters": {name: len(values) for name, values in parameters if values},
        }
    client = docker.from_env()
    # Remove a Batfish service this run created; stop one it restarted.
    owned = False
    restarted = False
    session_id = f"routing-spec-{uuid4().hex[:12]}"
    start_requested = False
    started = False

    def accept(report, path):
        report.write(path)
        if report.status != "passed" or any(
            row.status != "passed" for row in report.results
        ):
            raise RuntimeError(f"Requirements did not pass: {path}")
        return {"passed": len(report.results), "failed": 0}

    try:
        if batfish_verifier is None:
            try:
                restarted = (
                    client.containers.get(BATFISH_CONTAINER_NAME).status != "running"
                )
            except NotFound:
                owned = True
            ensure_batfish_service()
        verifier = batfish_verifier or BatfishVerifier()
        for name, contract in contracts.items():
            incident = next((i for i in spec.incidents if i.name == name), None)
            shutdown = []
            if incident:
                if native_srl:
                    endpoint = f"{incident.host_name}:{incident.intf_name}"
                    (link,) = [
                        link
                        for link in env.get_lab_spec().links
                        if endpoint in link.endpoints
                    ]
                    shutdown = [tuple(e.split(":")) for e in link.endpoints]
                else:
                    machine = env.lab.machines[incident.host_name]
                    link = machine.interfaces[
                        int(incident.intf_name.removeprefix("eth"))
                    ].link
                    shutdown = [
                        (n, f"eth{index}")
                        for n in link.machines
                        for index, interface in env.lab.machines[n].interfaces.items()
                        if interface.link.name == link.name
                    ]
            snapshot = snapshot_builder(
                env,
                contract,
                output / name,
                shutdown=shutdown,
                **(
                    {}
                    if native_srl or native_vendor
                    else {"routing_model": spec.routing_model}
                ),
            )
            summary["static"][name] = accept(
                verifier.verify(contract, snapshot), output / f"static-{name}.json"
            )
            system_logger.info(
                "static %s %s %s", spec.scenario, name, summary["static"][name]
            )
        for mutation in spec.counterexamples:
            configs = candidate_configs()
            if configs[mutation.node].count(mutation.old) != 1:
                raise ValueError(
                    "Counterexample must identify exactly one concrete configuration statement"
                )
            configs[mutation.node] = configs[mutation.node].replace(
                mutation.old, mutation.new, 1
            )
            snapshot = snapshot_builder(
                env,
                spec.healthy,
                output / mutation.name,
                configs=configs,
                **(
                    {}
                    if native_srl or native_vendor
                    else {"routing_model": spec.routing_model}
                ),
            )
            report = verifier.verify(spec.healthy, snapshot)
            report.write(output / f"counterexample-{mutation.name}.json")
            failed = {
                result.intent for result in report.results if result.status == "failed"
            }
            if (
                any(
                    result.status not in {"passed", "failed"}
                    for result in report.results
                )
                or any(result.status != "passed" for result in report.sanity)
                or not failed
                or failed != set(mutation.expected_failed_ids)
            ):
                raise RuntimeError(
                    f"Counterexample did not yield exactly its predeclared violations: {mutation.name}"
                )
            summary["counterexamples"][mutation.name] = {
                "status": "rejected",
                "failed_ids": sorted(failed),
            }
        predicted_failures = {}
        for failure in spec.failure_checks:
            if not failure.static_edits:
                continue
            configs = candidate_configs()
            for node, (old, new) in failure.static_edits.items():
                if configs[node].count(old) != 1:
                    raise ValueError(
                        "A modeled fault must identify one concrete statement per node"
                    )
                configs[node] = configs[node].replace(old, new, 1)
            snapshot = snapshot_builder(
                env,
                spec.healthy,
                output / ("model-" + failure.name),
                configs=configs,
                **(
                    {}
                    if native_srl or native_vendor
                    else {"routing_model": spec.routing_model}
                ),
            )
            report = verifier.verify(spec.healthy, snapshot)
            report.write(output / ("model-" + failure.name + ".json"))
            failed = {r.intent for r in report.results if r.status == "failed"}
            if (
                not set(failure.expected_failed_ids) <= failed
                or any(r.status not in {"passed", "failed"} for r in report.results)
                or any(r.status != "passed" for r in report.sanity)
            ):
                raise RuntimeError(
                    "Modeled fault did not satisfy its original mandatory violations"
                )
            if failure.requirements:
                positive = verifier.verify(failure.requirements, snapshot)
                accept(positive, output / ("model-" + failure.name + "-state.json"))
            predicted_failures[failure.name] = tuple(sorted(failed))
            summary.setdefault("failure_models", {})[failure.name] = {
                "mandatory_failed_ids": list(failure.expected_failed_ids),
                "predicted_failed_ids": sorted(failed),
            }
        if runtime:
            start_requested = True
            start_net_env(
                **scenario_start_kwargs(spec, parameters, configs=generated_configs),
                session_id=session_id,
                session_dir=str(output / "runtime"),
            )
            started = True
            session = Session().load_running_session(session_id)
            backend = runtime_for_session(session)

            def run_config(node, command):
                """Run a configuration operation; native routers commit slowly."""
                result = backend.exec(node, command, timeout=NATIVE_CONFIG_TIMEOUT_SEC)
                if result.startswith("[TIMEOUT]"):
                    raise RuntimeError(f"Configuration operation timed out on {node}")
                return result

            def check(name, contract):
                deadline = time.monotonic() + spec.convergence_timeout_sec
                while True:
                    report = runtime_verifier(
                        backend,
                        contract=contract,
                        plan=env.plan
                        if spec.routing_model
                        in {"srl-isp", "iosxr-isp", "routeros-isp"}
                        else None,
                    )
                    report.write(output / f"runtime-{name}.json")
                    if report.status == "passed" and all(
                        row.status == "passed" for row in report.results
                    ):
                        break
                    if time.monotonic() > deadline:
                        accept(report, output / f"runtime-{name}.json")
                    time.sleep(3)
                summary["runtime"][name] = {"passed": len(report.results), "failed": 0}
                system_logger.info(
                    "runtime %s %s %s", spec.scenario, name, summary["runtime"][name]
                )

            def reject(name, expected_failed_ids):
                contracts = [spec.healthy] + (
                    [spec.runtime_requirements] if spec.runtime_requirements else []
                )
                deadline = time.monotonic() + spec.convergence_timeout_sec
                while True:
                    reports = [
                        runtime_verifier(
                            backend,
                            contract=c,
                            plan=env.plan
                            if spec.routing_model
                            in {"srl-isp", "iosxr-isp", "routeros-isp"}
                            else None,
                        )
                        for c in contracts
                    ]
                    for index, report in enumerate(reports):
                        report.write(output / f"{name}-{index}.json")
                    results = [r for report in reports for r in report.results]
                    failed = {r.intent for r in results if r.status == "failed"}
                    if failed == set(expected_failed_ids) and all(
                        r.status in {"passed", "failed"} for r in results
                    ):
                        return sorted(failed)
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            f"Fault did not produce its predeclared original violations: {name}"
                        )
                    time.sleep(3)

            check("healthy", spec.healthy)
            if spec.runtime_requirements:
                check("healthy-additional", spec.runtime_requirements)
            for failure in spec.failure_checks:
                # These registered failures edit FRR configuration and have no
                # recovery API. Restore the declared candidate and restart FRR;
                # this is configuration restoration, not a protocol repair claim.
                originals = (
                    {}
                    if native_srl
                    else {
                        node: env.lab.machines[node].fs.readtext("etc/frr/frr.conf")
                        for node in failure.restore_nodes
                        if node not in native_routers
                    }
                )
                # Native routers restore the running configuration read before
                # injection, like FRR restores its candidate file.
                native_ops = {
                    node: native_platform(native_routers[node]).fault_ops(backend, node)
                    for node in failure.restore_nodes
                    if node in native_routers
                }
                native_originals = {
                    node: ops.save_config() for node, ops in native_ops.items()
                }
                nft_rules = {
                    node: backend.list_nft_ruleset(node)
                    for node in failure.restore_nft_nodes
                }
                srl_acl = (
                    {
                        node: backend.srl_bgp_acl_drop_179_entries(node)
                        for node in failure.restore_nodes
                    }
                    if native_srl and failure.problem == "bgp_acl_block"
                    else {}
                )
                try:
                    inject_failure(
                        [failure.problem],
                        session_id=session_id,
                        param_overrides=failure.params,
                    )
                    if failure.requirements:
                        check(
                            "failure-" + failure.name + "-state", failure.requirements
                        )
                    failed = reject(
                        "failure-" + failure.name,
                        predicted_failures.get(
                            failure.name, failure.expected_failed_ids
                        ),
                    )
                    summary.setdefault("failure_compatibility", {})[failure.name] = {
                        "problem": failure.problem,
                        "failed_ids": sorted(failed),
                    }
                finally:
                    if native_srl:
                        from nika.service.containerlab.srl_api import (
                            BGP_EXPORT_POLICY,
                            BGP_EXPORT_PREFIX_SET,
                        )

                        for node in failure.restore_nodes:
                            commands = ["enter candidate"]
                            if failure.problem == "bgp_asn_misconfig":
                                asn = next(
                                    n.asn
                                    for n in env.bgp_plan.nodes
                                    if n.device_name == node
                                )
                                commands.append(
                                    f"/network-instance default protocols bgp autonomous-system {asn}"
                                )
                            elif failure.problem == "bgp_missing_route_advertisement":
                                for peer in (
                                    p
                                    for p in env.bgp_plan.sessions
                                    if p.local_device == node
                                ):
                                    original_bgp = env.routing_documents[node][
                                        "srl_nokia-network-instance:network-instance"
                                    ][0]["protocols"]["srl_nokia-bgp:bgp"]
                                    original_peer = next(
                                        p
                                        for p in original_bgp["neighbor"]
                                        if p["peer-address"] == peer.remote_ip
                                    )
                                    original_af = next(
                                        (
                                            af
                                            for af in original_peer.get("afi-safi", [])
                                            if af["afi-safi-name"] == "ipv4-unicast"
                                        ),
                                        {},
                                    )
                                    policies = original_af.get("export-policy")
                                    path = f"/network-instance default protocols bgp neighbor {peer.remote_ip} afi-safi ipv4-unicast export-policy"
                                    commands.append(
                                        path
                                        + " [ "
                                        + " ".join(json.dumps(p) for p in policies)
                                        + " ]"
                                        if policies is not None
                                        else "delete " + path
                                    )
                                commands += [
                                    f"delete /routing-policy policy {BGP_EXPORT_POLICY}",
                                    f"delete /routing-policy prefix-set {BGP_EXPORT_PREFIX_SET}",
                                ]
                            elif failure.problem == "bgp_acl_block":
                                commands.extend(
                                    f"delete /acl acl-filter cpm type ipv4 entry {seq}"
                                    for seq in srl_acl[node]
                                )
                            else:
                                raise ValueError(
                                    "Unsupported SR Linux registered-fault restoration"
                                )
                            commands.append("commit now")
                            backend.srl_run_script(node, commands)
                    for node, rules in nft_rules.items():
                        backend.exec(
                            node,
                            "nft flush ruleset && printf %s "
                            + shlex.quote(rules)
                            + " | nft -f -",
                        )
                    for node, commands in failure.recovery_commands.items():
                        for command in commands:
                            backend.exec(node, command)
                    for node, config in originals.items():
                        backend.exec(
                            node,
                            f"printf %s {shlex.quote(config)} > /etc/frr/frr.conf && service frr restart",
                        )
                    for node, running in native_originals.items():
                        native_ops[node].restore_config(running)
                check(failure.name + "-restored", spec.healthy)
                if spec.runtime_requirements:
                    check(
                        failure.name + "-restored-additional", spec.runtime_requirements
                    )
            if native_vendor:
                from nika.net_env.routeros_simple_bgp.verify import (
                    CLI_COMMAND as routeros_cli,
                )

                inspection_command = (
                    "/pkg/bin/xr_cli 'show route'"
                    if spec.routing_model in {"iosxr-bgp", "iosxr-isp"}
                    else routeros_cli.format(command="/ip/route/print detail")
                )
            else:
                inspection_command = (
                    'sr_cli "show network-instance default route-table ipv4-unicast summary"'
                    if native_srl
                    else "vtysh -c 'show ip route json'"
                )
            node = next(
                (
                    intent.source
                    for intent in spec.healthy.intents
                    if intent.source is not None
                ),
                None,
            )
            if native_vendor:
                node = next(iter(env.BGP_ROUTERS))
            node = node if node is None or isinstance(node, str) else node.name
            if node in native_routers:
                inspection_command = native_validation(node).route_inspection_command
            if node:
                observed = backend.exec(node, inspection_command)
                (output / "runtime-route-inspection.json").write_text(observed)
            for mutation in spec.runtime_counterexamples:

                def configure(commands):
                    if native_srl:
                        result = backend.srl_run_script(mutation.node, commands)
                        with (
                            output / ("runtime-config-" + mutation.name + ".log")
                        ).open("a") as log:
                            log.write(result + "\n")
                        if "Parsing error:" in result:
                            raise RuntimeError(
                                f"SR Linux rejected the declared configuration operation: {result}"
                            )
                        return
                    if mutation.node in native_routers:
                        command = native_validation(mutation.node).config_command(
                            commands
                        )
                    elif native_vendor:
                        if spec.routing_model in {"iosxr-bgp", "iosxr-isp"}:
                            from nika.net_env.utils.iosxr.common import (
                                build_xr_config_command,
                            )

                            command = build_xr_config_command(commands)
                        else:
                            from nika.net_env.routeros_simple_bgp.verify import (
                                CLI_COMMAND,
                            )

                            command = CLI_COMMAND.format(command="; ".join(commands))
                    else:
                        command = shlex.join(
                            ["vtysh"]
                            + [item for line in commands for item in ("-c", line)]
                        )
                    with (output / ("runtime-config-" + mutation.name + ".log")).open(
                        "a"
                    ) as log:
                        log.write(run_config(mutation.node, command) + "\n")

                try:
                    configure(mutation.commands)
                    failed = reject(
                        "runtime-counterexample-" + mutation.name,
                        mutation.expected_failed_ids,
                    )
                    summary["counterexamples"][mutation.name] = {
                        "status": "rejected-in-runtime",
                        "failed_ids": sorted(failed),
                    }
                finally:
                    configure(mutation.recovery)
                check(mutation.name + "-recovered", spec.healthy)
                if spec.runtime_requirements:
                    check(
                        mutation.name + "-recovered-additional",
                        spec.runtime_requirements,
                    )
            for incident in spec.incidents:
                params = {
                    "host_name": incident.host_name,
                    "intf_name": incident.intf_name,
                }
                inject_failure(
                    ["link_down"], session_id=session_id, param_overrides=params
                )
                check(incident.name, incident.requirements)
                if incident.runtime_requirements:
                    check(incident.name + "-additional", incident.runtime_requirements)
                failure = LinkFailure(
                    scenario_name=spec.scenario, **session.scenario_params
                )
                # Carrier observation can lag the recovery action on native routers.
                if not wait_until(
                    lambda: failure.recover_fault(LinkFailureParams(**params))[
                        "verified"
                    ],
                    spec.convergence_timeout_sec,
                ):
                    raise RuntimeError("Link recovery failed")
                restored = (
                    []
                    if native_srl
                    else restore_link_routes(
                        env, backend, incident.host_name, incident.intf_name
                    )
                )
                (output / f"recovery-{incident.name}.json").write_text(
                    json.dumps(restored, indent=2) + "\n"
                )
                check(incident.name + "-recovered", spec.healthy)
                if spec.runtime_requirements:
                    check(
                        incident.name + "-recovered-additional",
                        spec.runtime_requirements,
                    )
    finally:
        try:
            if start_requested:
                try:
                    close_session(
                        session_id=session_id,
                        status="finished" if started else "error",
                    )
                except FileNotFoundError:
                    # Startup can fail before it has created a session record.
                    pass
        finally:
            try:
                if owned or restarted:
                    try:
                        container = client.containers.get(BATFISH_CONTAINER_NAME)
                        if owned:
                            container.remove(force=True)
                        else:
                            container.stop()
                    except NotFound:
                        pass
            finally:
                client.close()
    summary["status"] = "passed"
    summary["validation_scope"] = "static-and-runtime" if runtime else "static-only"
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
