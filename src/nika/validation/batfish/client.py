from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from nika.net_env.contract import ValidationSanityResult
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.compiler import BatfishQuestion


class BatfishClient:
    """Small pybatfish adapter that keeps Batfish types out of the verifier."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 9996) -> None:
        try:
            from pybatfish.client.session import Session
        except ImportError as exc:
            raise RuntimeError(
                "Batfish validation requires `uv sync --extra batfish`."
            ) from exc
        self._session = Session(host=host, port_v2=port)
        self._snapshot_name: str | None = None
        self._snapshot_metadata: dict[str, Any] = {}
        self._question_cache: dict[str, list[dict[str, Any]]] = {}

    def initialize(self, snapshot: ValidationSnapshot) -> dict[str, Any]:
        identity = snapshot.snapshot_id.replace(".", "_")
        network = f"nika_{identity}"
        snapshot_name = f"snapshot_{identity}"
        self._session.set_network(network)
        self._snapshot_metadata = snapshot.metadata
        self._question_cache.clear()
        self._snapshot_name = self._session.init_snapshot(
            str(snapshot.path), name=snapshot_name, overwrite=True
        )
        return {
            "network": network,
            "snapshot": self._snapshot_name,
            "components": dict(self._session.get_component_versions()),
        }

    def execute(self, question: BatfishQuestion) -> list[dict[str, Any]]:
        snapshot = self._require_snapshot()
        if question.kind in self._question_cache:
            return self._question_cache[question.kind]
        if question.kind == "reachability":
            from pybatfish.datamodel import HeaderConstraints, PathConstraints

            parameters = question.parameters
            path = PathConstraints(
                **{
                    key: value
                    for key, value in parameters["path_constraints"].items()
                    if value is not None
                }
            )
            headers = HeaderConstraints(**parameters["headers"])
            frame = (
                self._session.q.reachability(
                    actions=parameters["actions"],
                    headers=headers,
                    pathConstraints=path,
                    maxTraces=parameters["max_traces"],
                )
                .answer(snapshot)
                .frame()
            )
        elif question.kind == "bgp_adjacency":
            compatibility = (
                self._session.q.bgpSessionCompatibility().answer(snapshot).frame()
            )
            status = self._session.q.bgpSessionStatus().answer(snapshot).frame()
            rows = [
                {"analysis": "compatibility", **row} for row in _records(compatibility)
            ] + [{"analysis": "establishment", **row} for row in _records(status)]
            self._question_cache[question.kind] = rows
            return rows
        elif question.kind == "ospf_adjacency":
            frame = self._session.q.ospfSessionCompatibility().answer(snapshot).frame()
        else:  # pragma: no cover - compiler constrains this field
            raise ValueError(f"unknown Batfish question kind {question.kind!r}")
        rows = _records(frame)
        if question.kind == "ospf_adjacency":
            self._question_cache[question.kind] = rows
        return rows

    def sanity_checks(self) -> tuple[ValidationSanityResult, ...]:
        snapshot = self._require_snapshot()
        checks: list[ValidationSanityResult] = []
        started = time.monotonic()
        try:
            rows = _records(self._session.q.fileParseStatus().answer(snapshot).frame())
            bad = [
                row
                for row in rows
                if str(row.get("Status", "")).upper()
                not in {"PASSED", "PARTIALLY_UNRECOGNIZED"}
            ]
            expected_format = self._snapshot_metadata.get("snapshot_config_format")
            wrong_format = [
                row
                for row in rows
                if str(row.get("File_Name", "")).startswith("configs/")
                and expected_format
                and str(row.get("File_Format")) != expected_format
            ]
            invalid = bad + wrong_format
            warnings = _records(self._session.q.parseWarning().answer(snapshot).frame())
            checks.append(
                ValidationSanityResult(
                    check="configuration_parse",
                    status="failed" if invalid else "passed",
                    evidence={
                        "files": rows,
                        "invalid_files": invalid,
                        "parse_warnings": warnings[:100],
                        "parse_warning_count": len(warnings),
                    },
                    reason="Batfish could not parse one or more configuration files"
                    if invalid
                    else None,
                    duration_ms=_elapsed(started),
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded as verifier evidence
            checks.append(_sanity_error("configuration_parse", started, exc))

        from pybatfish.client import asserts

        assertion_checks: tuple[
            tuple[str, Callable[..., bool], dict[str, Any]], ...
        ] = (
            ("undefined_references", asserts.assert_no_undefined_references, {}),
            (
                "duplicate_router_ids",
                asserts.assert_no_duplicate_router_ids,
                {"protocols": ["bgp", "ospf"]},
            ),
            ("forwarding_loops", asserts.assert_no_forwarding_loops, {}),
        )
        from pybatfish.exception import BatfishAssertException

        for name, assertion, kwargs in assertion_checks:
            started = time.monotonic()
            try:
                assertion(
                    snapshot=snapshot,
                    session=self._session,
                    df_format="records",
                    **kwargs,
                )
                checks.append(
                    ValidationSanityResult(
                        check=name, status="passed", duration_ms=_elapsed(started)
                    )
                )
            except BatfishAssertException as exc:
                checks.append(
                    ValidationSanityResult(
                        check=name,
                        status="failed",
                        evidence={"counterexample": str(exc)},
                        reason=str(exc).splitlines()[0],
                        duration_ms=_elapsed(started),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - recorded as verifier evidence
                checks.append(_sanity_error(name, started, exc))
        return tuple(checks)

    def _require_snapshot(self) -> str:
        if self._snapshot_name is None:
            raise RuntimeError("Batfish snapshot has not been initialized")
        return self._snapshot_name


def _sanity_error(name: str, started: float, exc: Exception) -> ValidationSanityResult:
    return ValidationSanityResult(
        check=name,
        status="error",
        reason=str(exc),
        evidence={"error_type": type(exc).__name__},
        duration_ms=_elapsed(started),
    )


def _elapsed(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _records(frame: Any) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)
