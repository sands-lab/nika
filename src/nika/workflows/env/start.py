"""Start a network lab for one scenario and persist a new session."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from nika.net_env.net_env_pool import (
    get_net_env_instance,
    scenario_backend,
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


def _normalize_topo_size(raw: str | None) -> Literal["s", "m", "l"] | None:
    """Return ``None`` for missing/blank input; otherwise validate ``s``/``m``/``l``."""
    if raw is None or raw == "":
        return None
    if raw not in ("s", "m", "l"):
        raise ValueError("Topology size must be one of: s, m, l.")
    return raw  # type: ignore[return-value]


def start_net_env(
    scenario: str,
    topo_size: str | None,
    *,
    redeploy: bool = True,
    instance_tag: str | None = None,
    session_tag: str | None = None,
    result_dir: str | None = None,
) -> str:
    """Deploy the lab for ``scenario`` and create a new runtime session."""
    size = _normalize_topo_size(topo_size)
    if scenario_requires_topo_size(scenario) and size is None:
        raise ValueError(
            f"Scenario '{scenario}' requires an explicit topology size (-s s|m|l)."
        )
    if not scenario_requires_topo_size(scenario) and size is not None:
        raise ValueError(
            f"Scenario '{scenario}' does not use topology sizes; omit -s/--size."
        )

    backend = scenario_backend(scenario)

    refresh_logger()
    suffix = uuid4().hex[:6]
    tag = (
        f"{instance_tag}-{suffix}"
        if instance_tag
        else f"{datetime.now().strftime('%m%d%H%M%S')}-{suffix}"
    )
    lab_name = f"{scenario}__{tag}"
    session_id = make_session_id(session_tag=session_tag, suffix=suffix)
    net_env = get_net_env_instance(
        scenario, backend=backend, topo_size=size, lab_name=lab_name
    )
    if backend == "containerlab":
        net_env._ensure_runtime_files()

    session = Session()
    scenario_params: dict = {"lab_name": net_env.name, "backend": backend}
    if size is not None:
        scenario_params["topo_size"] = size
    topology_file = getattr(net_env, "topology_file", None)
    runtime_workdir = getattr(net_env, "runtime_workdir", None)
    session.init_session(
        session_id=session_id,
        scenario_name=scenario,
        lab_name=net_env.name,
        scenario_topo_size=size,
        scenario_params=scenario_params,
        result_dir=result_dir,
        backend=backend,
        topology_file=topology_file,
        runtime_workdir=runtime_workdir,
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
    except Exception as exc:
        event_type = "env_verify_failed" if net_env.lab_exists() else "env_start_failed"
        log_error_event(
            event_type,
            f"Failed to start network environment: {scenario} ({session_id}): {exc}",
            scenario=scenario,
            backend=backend,
            topo_size=size,
            session_id=session_id,
            lab_name=net_env.name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        # A failed start must not strand the lab we just deployed: with one
        # leaked lab per failed verification, a long benchmark run piles up
        # dozens of running container sets.
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
        f"Started network environment: {scenario} (backend={backend}, size={size}) — session {session_id}, lab {net_env.name}",
        scenario=scenario,
        backend=backend,
        topo_size=size,
        session_id=session_id,
        lab_name=net_env.name,
    )
    return session_id
