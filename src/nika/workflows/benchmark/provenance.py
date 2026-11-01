"""Run-record schema version and run provenance (contract D).

The run directory's ``run.json`` records the schema version of the records
this frontend wrote (``run.json``, ``nika.jsonl``, ``messages.jsonl``, and
``model_calls.jsonl``) and what produced the run: this frontend, the
benchmark engine and the engine API version they spoke, and the host that ran
the environments. The engine API version covers the files the engine writes.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from pydantic import BaseModel, ConfigDict

from nika.benchmark_engine.protocol import (
    API_VERSION,
    API_VERSIONS,
    EngineDescription,
    UnsupportedContractVersion,
)

# Run-record schema versions this frontend reads. Additive changes keep the
# version; a breaking change adds a new one here.
SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
SCHEMA_VERSION = max(SCHEMA_VERSIONS)
# Runs recorded before ``run.json`` carried a schema version.
UNVERSIONED_SCHEMA = 1


class _Record(BaseModel):
    # A newer frontend may add fields within a results schema version.
    model_config = ConfigDict(extra="allow")


class FrontendProvenance(_Record):
    version: str | None = None
    commit: str | None = None
    dirty: bool | None = None


class EngineProvenance(_Record):
    """``tag`` and ``image`` are set when the release pins a benchmark engine.

    ``api`` is the engine API version the frontend and engine spoke.
    """

    release: str | None = None
    api: int
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
    host: HostProvenance | None = None


def schema_version_of(run: dict[str, Any]) -> Any:
    """The run-record schema version a run directory's ``run.json`` records."""
    return run.get("schema_version", UNVERSIONED_SCHEMA)


def check_schema_version(schema: Any) -> None:
    """Raise ``UnsupportedContractVersion`` unless this NIKA reads *schema*."""
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise UnsupportedContractVersion(
            f"run-record schema version must be an integer, got {schema!r}"
        )
    if schema not in SCHEMA_VERSIONS:
        raise UnsupportedContractVersion(
            f"run-record schema version {schema} is not supported by this NIKA "
            f"(supported: {', '.join(str(v) for v in sorted(SCHEMA_VERSIONS))}). "
            "Upgrade NIKA to read these results."
        )


def _frontend_version() -> str | None:
    try:
        return version("nika")
    except PackageNotFoundError:
        return None


def build_provenance(
    engine: EngineDescription | None,
    *,
    commit: str | None,
    dirty: bool,
    pinned: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provenance of a run launched by this frontend at *commit*.

    *engine* is what ``describe`` reported for a release run. Without it (an
    ad-hoc task batch), the current NIKA's engine ran the trials in process.
    *pinned* holds the ``tag`` and ``image`` of a benchmark engine container.
    """
    from nika.benchmark_engine.host import engine_host_info

    if engine is None:
        engine_block = EngineProvenance(api=API_VERSION, commit=commit, dirty=dirty)
        host = engine_host_info()
    else:
        engine_block = EngineProvenance(
            release=engine.release_version,
            api=max(set(engine.api_versions) & API_VERSIONS),
            commit=engine.engine_commit,
            dirty=engine.engine_dirty,
            **(pinned or {}),
        )
        host = engine.host
    return Provenance(
        frontend=FrontendProvenance(
            version=_frontend_version(), commit=commit, dirty=dirty
        ),
        engine=engine_block,
        host=HostProvenance(**host.model_dump()) if host is not None else None,
    ).model_dump(mode="json")


def run_results_fields(
    engine: EngineDescription | None,
    *,
    commit: str | None,
    dirty: bool,
    pinned: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The contract D fields of a run directory's ``run.json``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": build_provenance(
            engine, commit=commit, dirty=dirty, pinned=pinned
        ),
    }
