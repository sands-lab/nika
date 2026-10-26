import json
import math
import re
import shlex
import time

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase
from nika.service.kathara.telemetry_api import time_bound_seconds
from nika.telemetry.management import TELEMETRY_COLLECTOR
from nika.telemetry.prometheus import PROMETHEUS_URL

PROMETHEUS_MAX_QUERY_CHARS = 1024
_STEP = re.compile(r"\d+(\.\d+)?[smh]?")
# Labels every series carries from the scrape config; ``node`` is kept apart.
_SCRAPE_LABELS = ("instance", "job")


class PrometheusAPIMixin:
    """Query device metrics stored by Prometheus on the lab telemetry collector."""

    def prometheus_query(
        self: _SupportsBase,
        query: str,
        start_time: str | None = None,
        end_time: str | None = None,
        step: str = "15s",
        limit: int = 50,
    ) -> list[dict]:
        """Return one row per series: an instant query, or a range with ``start_time``."""
        if not query.strip() or len(query) > PROMETHEUS_MAX_QUERY_CHARS:
            raise ValueError(
                f"query must be 1 to {PROMETHEUS_MAX_QUERY_CHARS} characters."
            )
        if int(limit) < 1:
            raise ValueError("limit must be at least 1.")
        if not _STEP.fullmatch(step):
            raise ValueError("step must be a duration such as 15s, 1m, or 30.")
        if start_time is None:
            path = "/api/v1/query"
            params = [("query", query)]
            if end_time is not None:
                params.append(("time", str(time_bound_seconds(end_time))))
        else:
            path = "/api/v1/query_range"
            end = time_bound_seconds(end_time) if end_time is not None else time.time()
            params = [
                ("query", query),
                ("start", str(time_bound_seconds(start_time))),
                ("end", str(end)),
                ("step", step),
            ]
        argv = ["curl", "-s", "-G", f"{PROMETHEUS_URL}{path}"]
        for key, value in params:
            argv += ["--data-urlencode", f"{key}={value}"]
        raw = self.exec_cmd(TELEMETRY_COLLECTOR, shlex.join(argv), timeout=30)
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"Prometheus is not answering: {raw[:300]}") from exc
        if body.get("status") != "success":
            raise ValueError(f"Prometheus query failed: {body.get('error', raw[:300])}")

        data = body["data"]
        if data["resultType"] in ("scalar", "string"):
            return [{"value": _number(data["result"][1])}]
        rows: list[dict] = []
        for series in data["result"][: int(limit)]:
            labels = dict(series["metric"])
            row: dict = {"node": labels.pop("node", None)}
            for label in _SCRAPE_LABELS:
                labels.pop(label, None)
            row["metric"] = labels
            if "value" in series:
                row["value"] = _number(series["value"][1])
            else:
                row["values"] = [
                    [point[0], _number(point[1])] for point in series["values"]
                ]
            rows.append(row)
        return rows


def _number(value: str) -> float | str:
    """Prometheus sends sample values as strings; ``NaN`` and ``+Inf`` stay strings."""
    try:
        number = float(value)
    except ValueError:
        return value
    return number if math.isfinite(number) else value


class _ExecPrometheus(PrometheusAPIMixin):
    """Bind ``PrometheusAPIMixin`` to any lab API that implements ``exec_cmd``."""

    def __init__(self, api) -> None:
        self._api = api

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        return self._api.exec_cmd(host_name, command, timeout=timeout)


def prometheus_query_for(api, **kwargs) -> list[dict]:
    """Query Prometheus through ``api.exec_cmd``."""
    return _ExecPrometheus(api).prometheus_query(**kwargs)


class KatharaPrometheusAPI(KatharaBaseAPI, PrometheusAPIMixin):
    """Kathara API for Prometheus device metrics."""

    pass
