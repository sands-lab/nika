import shlex
import time
from datetime import datetime

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase
from nika.service.kathara.telemetry_api import time_bound_seconds
from nika.telemetry.management import TELEMETRY_COLLECTOR
from nika.telemetry.syslog import SYSLOG_DIR, SYSLOG_HOSTS, parse_log_line

SYSLOG_MAX_CONTAINS = 256


def _line_seconds(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class SyslogAPIMixin:
    """Query syslog records stored by the lab telemetry collector."""

    def _syslog_hosts(self: _SupportsBase) -> dict[str, str]:
        raw = self.exec_cmd(TELEMETRY_COLLECTOR, f"cat {SYSLOG_HOSTS}")
        hosts: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                hosts[parts[1]] = parts[0]
        return hosts

    def syslog_query(
        self: _SupportsBase,
        start_time: str,
        end_time: str | None = None,
        host: str | None = None,
        program: str | None = None,
        contains: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return syslog rows in the time window, newest last."""
        if int(limit) < 1:
            raise ValueError("limit must be at least 1.")
        if contains is not None and len(contains) > SYSLOG_MAX_CONTAINS:
            raise ValueError(
                f"contains is longer than {SYSLOG_MAX_CONTAINS} characters."
            )
        hosts = self._syslog_hosts()
        names_by_ip = {ip: name for name, ip in hosts.items()}
        if host is not None and host not in hosts:
            raise ValueError(f"Unknown host {host!r}; known: {sorted(hosts)}.")
        start = time_bound_seconds(start_time)
        end = time_bound_seconds(end_time) if end_time is not None else time.time()
        path = (
            f"{SYSLOG_DIR}/{hosts[host]}.log" if host is not None else f"{SYSLOG_DIR}/*"
        )
        raw = self.exec_cmd(
            TELEMETRY_COLLECTOR,
            f"sh -c {shlex.quote('tail -n 5000 ' + path + ' 2>/dev/null || true')}",
            timeout=30,
        )
        program_filter = (program or "").strip().lower()
        needle = contains or ""
        rows: list[dict] = []
        for line in raw.splitlines():
            if line.startswith("==>"):
                continue
            parsed = parse_log_line(line)
            if parsed is None:
                continue
            stamp = _line_seconds(parsed["time"])
            if stamp is None or stamp < start or stamp > end:
                continue
            if host is not None:
                parsed["host"] = host
            else:
                parsed["host"] = names_by_ip.get(parsed["host"], parsed["host"])
            if program_filter and parsed["program"].lower() != program_filter:
                continue
            if needle and needle not in parsed["message"]:
                continue
            rows.append(parsed)
        return rows[-int(limit) :]


class _ExecSyslog(SyslogAPIMixin):
    """Bind ``SyslogAPIMixin`` to any lab API that implements ``exec_cmd``."""

    def __init__(self, api) -> None:
        self._api = api

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        return self._api.exec_cmd(host_name, command, timeout=timeout)


def syslog_query_for(api, **kwargs) -> list[dict]:
    """Query syslog records through ``api.exec_cmd``."""
    return _ExecSyslog(api).syslog_query(**kwargs)


class KatharaSyslogAPI(KatharaBaseAPI, SyslogAPIMixin):
    """Kathara API for syslog records."""

    pass
