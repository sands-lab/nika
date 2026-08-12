"""Start a network lab for one scenario and persist a new session."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from nika.net_env.isp.bgp.config import DEFAULT_BGP_MODE, normalize_bgp_mode
from nika.net_env.isp.bgp.errors import BgpConfigError
from nika.net_env.isp.igp.config import (
    DEFAULT_CONSTANT_METRIC,
    DEFAULT_IGP,
    DEFAULT_METRIC_STRATEGY,
    DEFAULT_TOPO,
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
    scenario_requires_topo_size,
)
from nika.net_env.verify import verify_lab_with_retry
from nika.utils.logger import (
    bind_session_dir,
    log_error_event,
    log_event,
    refresh_logger,
)
from nika.utils.session import Session
from nika.utils.session_id import make_session_id

ISP_SCENARIO = "isp"


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
        "device_profile": device_profile,
    }
    any_provided = any(value is not None for value in provided.values())
    if scenario != ISP_SCENARIO:
        if any_provided:
            raise ValueError(
                f"Scenario '{scenario}' does not accept --topo/--igp/"
                "--metric-strategy/--constant-metric/--bgp-mode/"
                "--device-profile; those flags are only valid for "
                f"'{ISP_SCENARIO}'."
            )
        return {}

    resolved_backend = resolve_scenario_backend(
        scenario,
        backend=backend,
        default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP,
    )
    resolved_topo = topo if topo is not None else DEFAULT_TOPO
    resolved_igp = igp if igp is not None else DEFAULT_IGP
    resolved_strategy = (
        metric_strategy if metric_strategy is not None else DEFAULT_METRIC_STRATEGY
    )
    resolved_metric = (
        constant_metric if constant_metric is not None else DEFAULT_CONSTANT_METRIC
    )
    try:
        resolved_bgp = normalize_bgp_mode(
            bgp_mode if bgp_mode is not None else DEFAULT_BGP_MODE
        )
    except BgpConfigError as exc:
        raise ValueError(str(exc)) from exc
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
        "topo": resolved_topo,
        "igp": resolved_igp,
        "metric_strategy": resolved_strategy,
        "constant_metric": resolved_metric,
        "bgp_mode": resolved_bgp,
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
    backend: str | None = None,
    device_profile: str | None = None,
) -> str:
    """Deploy the lab for ``scenario`` and create a new runtime session."""
    from nika.remote.config import is_remote_enabled

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
            backend=backend,
            device_profile=device_profile,
        )

    size = _normalize_topo_size(topo_size)
    if scenario_requires_topo_size(scenario) and size is None:
        raise ValueError(
            f"Scenario '{scenario}' requires an explicit topology size (-s s|m|l)."
        )
    if not scenario_requires_topo_size(scenario) and size is not None:
        raise ValueError(
            f"Scenario '{scenario}' does not use topology sizes; omit -s/--size."
        )

    isp_kwargs = _resolve_isp_kwargs(
        scenario,
        topo=topo,
        igp=igp,
        metric_strategy=metric_strategy,
        constant_metric=constant_metric,
        bgp_mode=bgp_mode,
        device_profile=device_profile,
        backend=backend,
    )

    default_backend = DEFAULT_BACKEND_FOR_ISP if scenario == ISP_SCENARIO else None
    resolved_backend = resolve_scenario_backend(
        scenario,
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
    lab_name = f"{scenario}__{tag}"
    resolved_session_id = session_id or make_session_id(
        session_tag=session_tag, suffix=suffix
    )
    net_env_kwargs: dict = {"lab_name": lab_name, **isp_kwargs}
    if size is not None:
        net_env_kwargs["topo_size"] = size
    net_env = get_net_env_instance(scenario, backend=resolved_backend, **net_env_kwargs)
    if resolved_backend == "containerlab":
        net_env._ensure_runtime_files()

    session = Session()
    scenario_params: dict = {"lab_name": net_env.name, "backend": resolved_backend}
    if size is not None:
        scenario_params["topo_size"] = size
    scenario_params.update(isp_kwargs)
    topology_file = getattr(net_env, "topology_file", None)
    runtime_workdir = getattr(net_env, "runtime_workdir", None)
    metadata = getattr(net_env, "metadata", None)
    session.init_session(
        session_id=resolved_session_id,
        scenario_name=scenario,
        lab_name=net_env.name,
        scenario_topo_size=size,
        scenario_params=scenario_params,
        result_dir=result_dir,
        session_dir=session_dir,
        backend=resolved_backend,
        topology_file=topology_file,
        runtime_workdir=runtime_workdir,
        metadata=metadata,
    )
    bind_session_dir(session.session_dir)

    try:
        if net_env.lab_exists() and redeploy:
            net_env.undeploy()
            net_env.deploy()
        elif not net_env.lab_exists():
            net_env.deploy()

        verify_result = verify_lab_with_retry(net_env)
        if verify_result is not None:
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
    except Exception as exc:
        event_type = "env_verify_failed" if net_env.lab_exists() else "env_start_failed"
        log_error_event(
            event_type,
            f"Failed to start network environment: {scenario} ({resolved_session_id}): {exc}",
            scenario=scenario,
            backend=resolved_backend,
            topo_size=size,
            session_id=resolved_session_id,
            lab_name=net_env.name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        try:
            net_env.undeploy()
        except Exception as cleanup_exc:  # noqa: BLE001 - best effort
            print(
                f"WARNING: could not undeploy lab {net_env.name} after "
                f"failed start: {cleanup_exc}"
            )
        raise

    log_event(
        "env_start",
        f"Started network environment: {scenario} (backend={resolved_backend}, size={size}) — session {resolved_session_id}, lab {net_env.name}",
        scenario=scenario,
        backend=resolved_backend,
        topo_size=size,
        session_id=resolved_session_id,
        lab_name=net_env.name,
        metadata=getattr(net_env, "metadata", None) or metadata,
    )
    return resolved_session_id
