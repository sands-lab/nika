"""Start a network lab for one scenario and persist a new session."""

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from nika.net_env.isp.bgp.config import (
    DEFAULT_BGP_MODE,
    ISP_BGP_MODES,
    normalize_bgp_mode,
)
from nika.net_env.contract import (
    VALIDATION_CONTRACT_FILENAME,
    VALIDATION_RESULTS_FILENAME,
    ValidationReport,
)
from nika.net_env.isp.bgp.errors import BgpConfigError
from nika.net_env.isp.igp.config import (
    DEFAULT_CONSTANT_METRIC,
    DEFAULT_IGP,
    DEFAULT_METRIC_STRATEGY,
    SUPPORTED_IGPS,
    SUPPORTED_METRIC_STRATEGIES,
)
from nika.net_env.isp.profiles import (
    DEFAULT_BACKEND_FOR_ISP,
    default_device_profile,
    normalize_device_profile,
    validate_backend_profile,
)
from nika.net_env.net_env_pool import (
    get_net_env_instance,
    resolve_scenario_backend,
    resolve_scenario_id,
    scenario_fixed_topo_size,
    scenario_requires_topo_size,
)
from nika.net_env.verify import verify_lab_with_retry
from nika.run_config.loader import get_run_config
from nika.utils.logger import (
    bind_session_dir,
    log_error_event,
    log_event,
    refresh_logger,
)
from nika.utils.session import Session
from nika.utils.session_id import make_session_id
from nika.net_env.isp.identity import (
    is_isp_base_topology,
    is_isp_named_special,
    is_isp_scenario,
)
from nika.workflows.validation.static import (
    STATIC_VALIDATION_FILENAME,
    run_static_validation,
)


def _normalize_topo_size(raw: str | None) -> Literal["s", "m", "l"] | None:
    """Return ``None`` for missing/blank input; otherwise validate ``s``/``m``/``l``."""
    if raw is None or raw == "":
        return None
    if raw not in ("s", "m", "l"):
        raise ValueError("Topology size must be one of: s, m, l.")
    return raw  # type: ignore[return-value]


def _resolve_isp_kwargs(
    scenario: str,
    *,
    topo: str | None,
    igp: str | None,
    metric_strategy: str | None,
    constant_metric: int | None,
    bgp_mode: str | None,
    rpki: bool | None = None,
    device_profile: str | None = None,
    backend: str | None = None,
) -> dict:
    """Validate ISP flags; return kwargs for ``get_net_env_instance``."""
    provided = {
        "topo": topo,
        "igp": igp,
        "metric_strategy": metric_strategy,
        "constant_metric": constant_metric,
        "bgp_mode": bgp_mode,
        "rpki": rpki,
        "device_profile": device_profile,
    }
    any_provided = any(value is not None for value in provided.values())

    if is_isp_named_special(scenario):
        # Case YAML / release rows often still carry device_profile=frr (and
        # backend=kathara). Those are the baked defaults — ignore them. Reject
        # only truly conflicting protocol knobs.
        conflicts: list[str] = []
        if topo is not None:
            conflicts.append(f"topo={topo!r}")
        if igp is not None:
            conflicts.append(f"igp={igp!r}")
        if metric_strategy is not None:
            conflicts.append(f"metric_strategy={metric_strategy!r}")
        if constant_metric is not None:
            conflicts.append(f"constant_metric={constant_metric!r}")
        if bgp_mode is not None:
            conflicts.append(f"bgp_mode={bgp_mode!r}")
        if rpki is not None:
            conflicts.append(f"rpki={rpki!r}")
        if device_profile not in (None, "", "-", "frr"):
            conflicts.append(f"device_profile={device_profile!r}")
        if conflicts:
            raise ValueError(
                f"Scenario '{scenario}' uses a fixed protocol profile; omit "
                "--topo/--igp/--metric-strategy/--constant-metric/--bgp-mode/"
                f"--rpki/--device-profile (got {', '.join(conflicts)})."
            )
        return {}

    if not is_isp_base_topology(scenario):
        if any_provided:
            raise ValueError(
                f"Scenario '{scenario}' does not accept --topo/--igp/"
                "--metric-strategy/--constant-metric/--bgp-mode/--rpki/"
                "--device-profile; those flags are only valid for "
                "ISP topology scenarios (e.g. isp_abilene)."
            )
        return {}

    if topo is not None:
        raise ValueError(
            f"Scenario '{scenario}' bakes topology into the scenario name; "
            "omit --topo."
        )
    if rpki:
        raise ValueError(
            "RPKI requires a named scenario "
            "(isp_abilene_ebgp_rpki or isp_geant_ebgp_rpki); omit --rpki."
        )

    resolved_backend = resolve_scenario_backend(
        scenario,
        backend=backend,
        default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP,
    )
    resolved_igp = igp if igp is not None else DEFAULT_IGP
    resolved_strategy = (
        metric_strategy if metric_strategy is not None else DEFAULT_METRIC_STRATEGY
    )
    resolved_metric = (
        constant_metric if constant_metric is not None else DEFAULT_CONSTANT_METRIC
    )
    raw_bgp = bgp_mode if bgp_mode is not None else DEFAULT_BGP_MODE
    try:
        resolved_bgp = normalize_bgp_mode(raw_bgp)
    except BgpConfigError as exc:
        raise ValueError(str(exc)) from exc
    if resolved_bgp not in ISP_BGP_MODES:
        raise ValueError(
            f"Unsupported bgp_mode {resolved_bgp!r}; expected one of {ISP_BGP_MODES}."
        )
    if resolved_igp not in SUPPORTED_IGPS:
        raise ValueError(
            f"Unsupported IGP {resolved_igp!r}; expected one of {SUPPORTED_IGPS}."
        )
    if resolved_strategy not in SUPPORTED_METRIC_STRATEGIES:
        raise ValueError(
            f"Unsupported metric strategy {resolved_strategy!r}; "
            f"expected one of {SUPPORTED_METRIC_STRATEGIES}."
        )
    if resolved_metric < 1:
        raise ValueError(f"constant_metric must be >= 1, got {resolved_metric}.")

    if device_profile is None:
        resolved_profile = default_device_profile(resolved_backend)
    else:
        try:
            resolved_profile = normalize_device_profile(device_profile)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    try:
        validate_backend_profile(resolved_backend, resolved_profile)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    return {
        "igp": resolved_igp,
        "metric_strategy": resolved_strategy,
        "constant_metric": resolved_metric,
        "bgp_mode": resolved_bgp,
        "rpki": False,
        "device_profile": resolved_profile,
    }


def start_net_env(
    scenario: str,
    topo_size: str | None,
    *,
    redeploy: bool = True,
    instance_tag: str | None = None,
    session_tag: str | None = None,
    result_dir: str | None = None,
    session_id: str | None = None,
    session_dir: str | None = None,
    topo: str | None = None,
    igp: str | None = None,
    metric_strategy: str | None = None,
    constant_metric: int | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
    backend: str | None = None,
    device_profile: str | None = None,
    static_validation: bool | None = None,
) -> str:
    """Deploy the lab for ``scenario`` and create a new runtime session."""
    from nika.remote.config import is_remote_enabled

    canonical = resolve_scenario_id(scenario)
    static_validation_enabled = (
        static_validation
        if static_validation is not None
        else get_run_config().nika.static_validation.enabled
    )

    if is_remote_enabled():
        from nika.remote.workflows import remote_start_net_env

        return remote_start_net_env(
            scenario,
            topo_size,
            redeploy=redeploy,
            instance_tag=instance_tag,
            session_tag=session_tag,
            result_dir=result_dir,
            session_id=session_id,
            session_dir=session_dir,
            topo=topo,
            igp=igp,
            metric_strategy=metric_strategy,
            constant_metric=constant_metric,
            bgp_mode=bgp_mode,
            rpki=rpki,
            backend=backend,
            device_profile=device_profile,
            static_validation=static_validation_enabled,
        )

    size = _normalize_topo_size(topo_size)
    if scenario_requires_topo_size(canonical) and size is None:
        raise ValueError(
            f"Scenario '{scenario}' requires an explicit topology size (-s s|m|l)."
        )
    if not scenario_requires_topo_size(canonical) and size is not None:
        raise ValueError(
            f"Scenario '{scenario}' does not use topology sizes; omit -s/--size."
        )
    # Fixed ISP scenarios still record s/m/l metadata for session/sampling.
    recorded_size = size if size is not None else scenario_fixed_topo_size(canonical)

    isp_kwargs = _resolve_isp_kwargs(
        canonical,
        topo=topo,
        igp=igp,
        metric_strategy=metric_strategy,
        constant_metric=constant_metric,
        bgp_mode=bgp_mode,
        rpki=rpki,
        device_profile=device_profile,
        backend=backend,
    )

    default_backend = DEFAULT_BACKEND_FOR_ISP if is_isp_scenario(canonical) else None
    resolved_backend = resolve_scenario_backend(
        canonical,
        backend=backend,
        default_when_ambiguous=default_backend,
    )

    refresh_logger()
    suffix = uuid4().hex[:6]
    tag = (
        f"{instance_tag}-{suffix}"
        if instance_tag
        else f"{datetime.now().strftime('%m%d%H%M%S')}-{suffix}"
    )
    lab_name = f"{canonical}__{tag}"
    resolved_session_id = session_id or make_session_id(
        session_tag=session_tag, suffix=suffix
    )
    net_env_kwargs: dict = {"lab_name": lab_name, **isp_kwargs}
    # Do not pass topo_size into ISP labs (topology is baked into scenario ID).
    if size is not None and not is_isp_scenario(canonical):
        net_env_kwargs["topo_size"] = size
    net_env = get_net_env_instance(
        canonical, backend=resolved_backend, **net_env_kwargs
    )
    if resolved_backend == "kathara":
        net_env.load_machines()
    if resolved_backend == "containerlab":
        net_env._ensure_runtime_files()

    session = Session()
    scenario_params: dict = {"lab_name": net_env.name, "backend": resolved_backend}
    if recorded_size is not None:
        scenario_params["topo_size"] = recorded_size
    if is_isp_scenario(canonical):
        scenario_params["topo"] = getattr(net_env, "topo", None)
    scenario_params.update(isp_kwargs)
    topology_file = getattr(net_env, "topology_file", None)
    runtime_workdir = getattr(net_env, "runtime_workdir", None)
    metadata = getattr(net_env, "metadata", None)
    session.init_session(
        session_id=resolved_session_id,
        scenario_name=canonical,
        lab_name=net_env.name,
        scenario_topo_size=recorded_size,
        scenario_params=scenario_params,
        result_dir=result_dir,
        session_dir=session_dir,
        backend=resolved_backend,
        topology_file=topology_file,
        runtime_workdir=runtime_workdir,
        metadata=metadata,
    )
    bind_session_dir(session.session_dir)

    contract = net_env.get_validation_contract()
    if contract is not None:
        contract_path = contract.write(
            Path(session.session_dir) / VALIDATION_CONTRACT_FILENAME
        )
        session.update_session("validation_contract", contract_path.name)
        log_event(
            "validation_contract_created",
            f"Saved validation contract for {scenario} ({resolved_session_id})",
            scenario=scenario,
            session_id=resolved_session_id,
            contract_id=contract.contract_id,
            intent_count=len(contract.intents),
            path=str(contract_path),
        )

    try:
        if static_validation_enabled and contract is not None:
            static_reports = run_static_validation(
                net_env=net_env,
                contract=contract,
                artifact_dir=session.session_dir,
            )
            for verifier, report in static_reports.items():
                filename = STATIC_VALIDATION_FILENAME.format(verifier=verifier)
                session.update_session(f"validation_{verifier}", filename)
                log_event(
                    f"{verifier}_validation",
                    f"{verifier.capitalize()} validation {report.status} "
                    f"({resolved_session_id})",
                    scenario=scenario,
                    session_id=resolved_session_id,
                    verifier=verifier,
                    status=report.status,
                    coverage=report.coverage.model_dump(mode="json")
                    if report.coverage
                    else None,
                    path=str(Path(session.session_dir) / filename),
                )
                if report.status in {"failed", "error"}:
                    raise RuntimeError(
                        f"{verifier} static validation {report.status}; see {filename}"
                    )

        if net_env.lab_exists() and redeploy:
            net_env.undeploy()
            net_env.deploy()
        elif not net_env.lab_exists():
            net_env.deploy()

        if hasattr(net_env, "preload_workload_images"):
            try:
                net_env.preload_workload_images()
            except Exception as preload_exc:  # noqa: BLE001 - fallback to network pulls
                log_error_event(
                    "env_preload_failed",
                    f"Workload image preload failed for {scenario} ({net_env.name}): {preload_exc}",
                    scenario=scenario,
                    session_id=resolved_session_id,
                    lab_name=net_env.name,
                    error=str(preload_exc),
                    error_type=type(preload_exc).__name__,
                )

        if hasattr(net_env, "sync_client_hosts"):
            try:
                net_env.sync_client_hosts()
            except Exception as sync_exc:  # noqa: BLE001 - verify will retry sync
                log_error_event(
                    "env_sync_hosts_failed",
                    f"Client hosts sync failed for {scenario} ({net_env.name}): {sync_exc}",
                    scenario=scenario,
                    session_id=resolved_session_id,
                    lab_name=net_env.name,
                    error=str(sync_exc),
                    error_type=type(sync_exc).__name__,
                )

        verify_result = verify_lab_with_retry(net_env)
        if verify_result is not None:
            validation_payload = (verify_result.get("details") or {}).get("validation")
            if validation_payload is not None:
                report = ValidationReport.model_validate(validation_payload)
                result_path = report.write(
                    Path(session.session_dir) / VALIDATION_RESULTS_FILENAME
                )
                session.update_session("validation_results", result_path.name)
                log_event(
                    "runtime_validation",
                    f"Runtime validation {report.status} ({resolved_session_id})",
                    scenario=scenario,
                    session_id=resolved_session_id,
                    verifier=report.verifier,
                    status=report.status,
                    validation=validation_payload,
                    path=str(result_path),
                )
            log_event(
                "env_verify",
                f"Lab verification passed for {scenario} ({net_env.name})",
                scenario=scenario,
                lab_name=net_env.name,
                checks=verify_result.get("checks"),
            )

        try:
            net_env.post_deploy()
            # post_deploy may enrich metadata (e.g. kubeconfig_path); persist it.
            session.update_session("metadata", dict(net_env.metadata or {}))
        except Exception as post_deploy_exc:  # noqa: BLE001 - must not fail an otherwise-verified deploy
            log_error_event(
                "env_post_deploy_failed",
                f"Post-deploy step failed for {scenario} ({resolved_session_id}): {post_deploy_exc}",
                scenario=scenario,
                session_id=resolved_session_id,
                error=str(post_deploy_exc),
                error_type=type(post_deploy_exc).__name__,
            )
    except BaseException as exc:
        # Session is registered before deploy; clean up so interrupted starts
        # do not leave a running lab (KeyboardInterrupt is not Exception).
        if isinstance(exc, Exception):
            event_type = (
                "env_verify_failed" if net_env.lab_exists() else "env_start_failed"
            )
            log_error_event(
                event_type,
                f"Failed to start network environment: {scenario} ({resolved_session_id}): {exc}",
                scenario=scenario,
                backend=resolved_backend,
                topo_size=recorded_size,
                session_id=resolved_session_id,
                lab_name=net_env.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        else:
            log_error_event(
                "env_start_interrupted",
                f"Interrupted while starting network environment: {scenario} ({resolved_session_id}): {type(exc).__name__}",
                scenario=scenario,
                backend=resolved_backend,
                topo_size=recorded_size,
                session_id=resolved_session_id,
                lab_name=net_env.name,
                error=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
            )
        try:
            from nika.workflows.session.close import (
                close_session,
                remove_orphaned_containerlab_management_network,
            )

            close_session(session_id=resolved_session_id, undeploy=True)
            remove_orphaned_containerlab_management_network(
                getattr(net_env, "name", None)
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - best effort
            print(
                f"WARNING: could not close session {resolved_session_id} after "
                f"failed start: {cleanup_exc}"
            )
            try:
                if net_env.lab_exists():
                    net_env.undeploy()
            except Exception as undeploy_exc:  # noqa: BLE001 - best effort
                print(
                    f"WARNING: could not undeploy lab {net_env.name} after "
                    f"failed start: {undeploy_exc}"
                )
            try:
                from nika.workflows.session.close import (
                    remove_orphaned_containerlab_management_network,
                )

                remove_orphaned_containerlab_management_network(
                    getattr(net_env, "name", None)
                )
            except Exception:  # noqa: BLE001 - best effort
                pass
        raise

    log_event(
        "env_start",
        f"Started network environment: {scenario} (backend={resolved_backend}, size={recorded_size}) — session {resolved_session_id}, lab {net_env.name}",
        scenario=scenario,
        backend=resolved_backend,
        topo_size=recorded_size,
        session_id=resolved_session_id,
        lab_name=net_env.name,
        metadata=getattr(net_env, "metadata", None) or metadata,
    )
    return resolved_session_id
