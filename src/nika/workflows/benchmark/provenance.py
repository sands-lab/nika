"""Results schema version and run provenance (contract D).

The run directory's ``run.json`` records which results schema the run was
written in and what produced it: this frontend, the benchmark engine, the
lifecycle protocol version they spoke, and the host that ran the environments.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from pydantic import BaseModel, ConfigDict

from nika.benchmark_engine.protocol import (
    PROTOCOL_VERSION,
    PROTOCOL_VERSIONS,
    RESULTS_SCHEMA_VERSION,
    RESULTS_SCHEMA_VERSIONS,
    EngineDescription,
    UnsupportedContractVersion,
)

# Results recorded before ``run.json`` carried a schema version.
UNVERSIONED_RESULTS_SCHEMA = 1


class _Record(BaseModel):
    # A newer frontend may add fields within a results schema version.
    model_config = ConfigDict(extra="allow")


class FrontendProvenance(_Record):
    version: str | None = None
    commit: str | None = None
    dirty: bool | None = None


class EngineProvenance(_Record):
    """``tag`` and ``image`` are set when the release pins a benchmark engine."""

    release: str | None = None
    tag: str | None = None
    commit: str | None = None
    dirty: bool | None = None
    image: str | None = None


class HostProvenance(_Record):
    docker: str | None = None
    kernel: str | None = None


class Provenance(_Record):
    frontend: FrontendProvenance
    engine: EngineProvenance
    protocol: int
    host: HostProvenance | None = None


def results_schema_of(run: dict[str, Any]) -> Any:
    """The results schema version a run directory's ``run.json`` records."""
    return run.get("results_schema", UNVERSIONED_RESULTS_SCHEMA)


def check_results_schema(schema: Any) -> None:
    """Raise ``UnsupportedContractVersion`` unless this NIKA reads *schema*."""
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise UnsupportedContractVersion(
            f"results schema version must be an integer, got {schema!r}"
        )
    if schema not in RESULTS_SCHEMA_VERSIONS:
        raise UnsupportedContractVersion(
            f"results schema version {schema} is not supported by this NIKA "
            f"(supported: {', '.join(str(v) for v in sorted(RESULTS_SCHEMA_VERSIONS))}). "
            "Upgrade NIKA to read these results."
        )


def _frontend_version() -> str | None:
    try:
        return version("nika")
    except PackageNotFoundError:
        return None


def build_provenance(
    engine: EngineDescription | None, *, commit: str | None, dirty: bool
) -> dict[str, Any]:
    """Provenance of a run launched by this frontend at *commit*.

    *engine* is what ``describe`` reported for a release run. Without it (an
    ad-hoc task batch), the current NIKA's engine ran the trials in process.
    """
    from nika.benchmark_engine.host import engine_host_info

    if engine is None:
        engine_block = EngineProvenance(commit=commit, dirty=dirty)
        host = engine_host_info()
        protocol = PROTOCOL_VERSION
    else:
        engine_block = EngineProvenance(
            release=engine.release_version,
            commit=engine.engine_commit,
            dirty=engine.engine_dirty,
        )
        host = engine.host
        protocol = max(set(engine.protocol_versions) & PROTOCOL_VERSIONS)
    return Provenance(
        frontend=FrontendProvenance(
            version=_frontend_version(), commit=commit, dirty=dirty
        ),
        engine=engine_block,
        protocol=protocol,
        host=HostProvenance(**host.model_dump()) if host is not None else None,
    ).model_dump(mode="json")


def run_results_fields(
    engine: EngineDescription | None, *, commit: str | None, dirty: bool
) -> dict[str, Any]:
    """The contract D fields of a run directory's ``run.json``."""
    return {
        "results_schema": RESULTS_SCHEMA_VERSION,
        "provenance": build_provenance(engine, commit=commit, dirty=dirty),
    }
