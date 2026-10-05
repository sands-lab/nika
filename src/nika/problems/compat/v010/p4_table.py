"""BMv2 ``simple_switch_CLI`` table faults retained for the 0.1.0 P4 labs."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource

_MISCONFIG_PORT = "99"
_MISCONFIG_MAC = "ff:ff:ff:ff:ff:ff"


def _cli_run(runtime, host: str, command: str) -> str:
    return runtime.exec(host, f"simple_switch_CLI <<< '{command}' 2>/dev/null")


def _show_match_tables(runtime, host: str) -> list[str]:
    tables: list[str] = []
    for line in _cli_run(runtime, host, "show_tables").splitlines():
        line = line.strip().removeprefix("RuntimeCmd:").strip()
        if "mk=" not in line or "mk=]" in line or "[" not in line:
            continue
        tables.append(line.split()[0])
    return tables


def _parse_action(dump_output: str) -> tuple[str, list[str]]:
    for line in dump_output.splitlines():
        if "Action entry:" not in line:
            continue
        after = line.split("Action entry:", 1)[1].strip()
        match = re.match(r"^(.+?)\s+-\s*(.*)$", after)
        if not match:
            raise RuntimeError(f"Could not parse action entry line: {line}")
        action = match.group(1).rsplit(".", 1)[-1].strip()
        params = [p.strip() for p in match.group(2).split(",") if p.strip()]
        return action, params
    raise RuntimeError("No action entry found in table dump")


def _populated_tables(runtime, host: str) -> list[tuple[str, list[str]]]:
    populated: list[tuple[str, list[str]]] = []
    for table in _show_match_tables(runtime, host):
        if "Dumping entry" not in _cli_run(runtime, host, f"table_dump {table}"):
            continue
        try:
            _, params = _parse_action(
                _cli_run(runtime, host, f"table_dump_entry {table} 0")
            )
        except RuntimeError:
            continue
        populated.append((table, params))
    return populated


def _selection_score(table: str, params: list[str]) -> int:
    lower = table.lower()
    score = 0
    if any(t in lower for t in ("forward", "lpm", "mpls", "dmac", "route", "fec")):
        score += 10
    if any(t in lower for t in ("check_", "border", "set_")):
        score -= 20
    if params:
        score += 5
    if any(len(p.replace(":", "")) >= 6 for p in params):
        score += 3
    if lower.startswith("myegress."):
        score -= 5
    return score


def _pick_table(runtime, host: str, *, require_action_params: bool = False) -> str:
    populated = _populated_tables(runtime, host)
    if require_action_params:
        populated = [item for item in populated if item[1]]
    if not populated:
        raise RuntimeError(f"No populated match table found on {host}")
    populated.sort(key=lambda item: _selection_score(*item), reverse=True)
    return populated[0][0]


def _reload_table(runtime, host: str, table: str) -> None:
    _cli_run(runtime, host, f"table_clear {table}")
    runtime.exec(host, "simple_switch_CLI < /commands.txt >/dev/null 2>&1 || true")


class P4TableParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableEntryMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_table_entry_missing"
    description = "A required P4 forwarding table entry is missing."
    TAGS = ["p4"]
    Params = P4TableParams

    def __init__(self, scenario_name: str | None = None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._table: str | None = None

    def root_cause_resources(self, params: P4TableParams):
        return [node_resource(params.host_name)]

    def _empty(self, host: str, table: str) -> bool:
        return "Dumping entry" not in _cli_run(
            self.runtime, host, f"table_dump {table}"
        )

    def inject_fault(self, params: P4TableParams):
        self._table = _pick_table(self.runtime, params.host_name)
        _cli_run(self.runtime, params.host_name, f"table_clear {self._table}")

    def verify_fault(self, params: P4TableParams) -> dict:
        host = params.host_name
        empty = bool(self._table) and self._empty(host, self._table)
        return build_verify_result(
            self.root_cause_name, empty, {"host": host, "table_name": self._table}
        )

    def recover_fault(self, params: P4TableParams) -> dict:
        host = params.host_name
        if self._table:
            _reload_table(self.runtime, host, self._table)
        restored = bool(self._table) and not self._empty(host, self._table)
        return build_verify_result(
            self.root_cause_name, restored, {"host": host, "table_name": self._table}
        )


class P4TableEntryMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_table_entry_misconfig"
    description = "A P4 forwarding table entry is misconfigured."
    TAGS = ["p4"]
    Params = P4TableParams

    def __init__(self, scenario_name: str | None = None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._details: dict | None = None

    def root_cause_resources(self, params: P4TableParams):
        return [node_resource(params.host_name)]

    def _entry(self, host: str) -> tuple[str, list[str]]:
        assert self._details is not None
        dump = _cli_run(
            self.runtime, host, f"table_dump_entry {self._details['table_name']} 0"
        )
        return _parse_action(dump)

    def inject_fault(self, params: P4TableParams):
        host = params.host_name
        table = _pick_table(self.runtime, host, require_action_params=True)
        action, original = _parse_action(
            _cli_run(self.runtime, host, f"table_dump_entry {table} 0")
        )
        corrupted = [
            _MISCONFIG_MAC if len(p.replace(":", "")) >= 6 else _MISCONFIG_PORT
            for p in original
        ] or [_MISCONFIG_PORT]
        _cli_run(
            self.runtime,
            host,
            f"table_modify {table} {action} 0 " + " ".join(corrupted),
        )
        self._details = {"table_name": table, "action_name": action}
        self._details["original_params"] = original
        self._details["expected_params"] = self._entry(host)[1]

    def verify_fault(self, params: P4TableParams) -> dict:
        verified = (
            self._details is not None
            and self._entry(params.host_name)
            == (self._details["action_name"], self._details["expected_params"])
            and self._details["expected_params"] != self._details["original_params"]
        )
        return build_verify_result(
            self.root_cause_name,
            verified,
            {"host": params.host_name, "misconfig_details": self._details},
        )

    def recover_fault(self, params: P4TableParams) -> dict:
        host = params.host_name
        if self._details:
            _reload_table(self.runtime, host, self._details["table_name"])
        restored = (
            self._details is not None
            and self._entry(host)[1] == self._details["original_params"]
        )
        return build_verify_result(
            self.root_cause_name,
            restored,
            {"host": host, "misconfig_details": self._details},
        )
