import re
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    ProblemBase,
    build_verify_result,
)
from nika.utils.logger import system_logger

logger = system_logger

_MISCONFIG_PORT = "99"
_MISCONFIG_MAC = "ff:ff:ff:ff:ff:ff"


def _cli_run(runtime, host: str, command: str) -> str:
    return runtime.exec(host, f"simple_switch_CLI <<< '{command}' 2>/dev/null")


def _cli_show_match_tables(runtime, host: str) -> list[str]:
    output = _cli_run(runtime, host, "show_tables")
    tables: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("RuntimeCmd:"):
            line = line[len("RuntimeCmd:") :].strip()
        if not line or line in {"Done", "Obtaining JSON from switch..."}:
            continue
        if "mk=" not in line or "mk=]" in line or "[" not in line:
            continue
        name = line.split()[0]
        tables.append(name)
    return tables


def _cli_table_has_match_entries(runtime, host: str, table_name: str) -> bool:
    dump = _cli_run(runtime, host, f"table_dump {table_name}")
    return "Dumping entry" in dump


def _table_selection_score(table_name: str, action_params: list[str]) -> int:
    lower = table_name.lower()
    score = 0
    if any(
        token in lower for token in ("forward", "lpm", "mpls", "dmac", "route", "fec")
    ):
        score += 10
    if any(token in lower for token in ("check_", "border", "set_")):
        score -= 20
    if action_params:
        score += 5
    if any(len(param.replace(":", "")) >= 6 for param in action_params):
        score += 3
    if lower.startswith("myegress."):
        score -= 5
    return score


def _list_populated_match_tables(runtime, host: str) -> list[tuple[str, list[str]]]:
    populated: list[tuple[str, list[str]]] = []
    for table_name in _cli_show_match_tables(runtime, host):
        if not _cli_table_has_match_entries(runtime, host, table_name):
            continue
        dump = _cli_run(runtime, host, f"table_dump_entry {table_name} 0")
        try:
            _, action_params = _parse_action_from_dump_entry(dump)
        except RuntimeError:
            continue
        populated.append((table_name, action_params))
    return populated


def _find_table_with_entries(
    runtime, host: str, *, require_action_params: bool = False
) -> str:
    populated = _list_populated_match_tables(runtime, host)
    if require_action_params:
        populated = [(name, params) for name, params in populated if params]
    if not populated:
        raise RuntimeError(f"No populated match table found on {host}")
    populated.sort(
        key=lambda item: _table_selection_score(item[0], item[1]), reverse=True
    )
    return populated[0][0]


def _parse_action_from_dump_entry(dump_output: str) -> tuple[str, list[str]]:
    for line in dump_output.splitlines():
        if "Action entry:" not in line:
            continue
        after = line.split("Action entry:", 1)[1].strip()
        match = re.match(r"^(.+?)\s+-\s*(.*)$", after)
        if not match:
            raise RuntimeError(f"Could not parse action entry line: {line}")
        action_name = match.group(1).rsplit(".", 1)[-1].strip()
        params = [param.strip() for param in match.group(2).split(",") if param.strip()]
        return action_name, params
    raise RuntimeError("No action entry found in table dump")


def _corrupt_action_param(param: str) -> str:
    cleaned = param.replace(":", "")
    if len(cleaned) >= 6:
        return _MISCONFIG_MAC
    return _MISCONFIG_PORT


def _misconfigure_first_table_entry(runtime, host: str) -> dict:
    table_name = _find_table_with_entries(runtime, host, require_action_params=True)
    dump_before = _cli_run(runtime, host, f"table_dump_entry {table_name} 0")
    action_name, params = _parse_action_from_dump_entry(dump_before)
    corrupted_params = (
        [_corrupt_action_param(param) for param in params]
        if params
        else [_MISCONFIG_PORT]
    )
    modify_cmd = f"table_modify {table_name} {action_name} 0 " + " ".join(
        corrupted_params
    )
    _cli_run(runtime, host, modify_cmd)
    dump_after = _cli_run(runtime, host, f"table_dump_entry {table_name} 0")
    _, expected_params = _parse_action_from_dump_entry(dump_after)
    return {
        "table_name": table_name,
        "entry_handle": 0,
        "action_name": action_name,
        "expected_params": expected_params,
    }


def _entry_matches_misconfig(dump_output: str, expected: dict) -> bool:
    action_name, params = _parse_action_from_dump_entry(dump_output)
    return (
        action_name == expected["action_name"] and params == expected["expected_params"]
    )


def _detect_misconfigured_entry(runtime, host: str) -> tuple[bool, str | None]:
    for table_name, _ in _list_populated_match_tables(runtime, host):
        dump = _cli_run(runtime, host, f"table_dump_entry {table_name} 0")
        if "ffffffffffff" in dump.lower():
            return True, table_name
        try:
            _, params = _parse_action_from_dump_entry(dump)
        except RuntimeError:
            continue
        if any(param.lower() in {"63", "99"} for param in params):
            return True, table_name
    return False, None


# ==================================================================
# Problem: P4 header definition error
# ==================================================================


class P4HeaderDefinitionErrorParams(BaseModel):
    """Parameters for injecting a P4 header definition error fault."""

    host_name: str = Field(description="Target BMv2 switch name.")
    p4_name: Optional[str] = Field(
        default=None,
        description="P4 program name (without suffix). Defaults to runtime detection.",
    )


class P4HeaderDefinitionError(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.SOFTWARE
    symptom = FailureSymptom.DOWN
    scope = FailureScope.NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "p4_header_definition_error"
    TAGS: str = ["p4"]

    Params = P4HeaderDefinitionErrorParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: P4HeaderDefinitionErrorParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4HeaderDefinitionErrorParams):
        p4_name = (
            params.p4_name
            if params.p4_name is not None
            else self.runtime.exec(params.host_name, "echo *.p4 | sed 's/\\.p4//'")
        )
        self.runtime.exec(
            params.host_name,
            f"cp {p4_name}.p4 {p4_name}.p4.bak && "
            f"rm {p4_name}.json && "
            f"sed -Ei "
            f"-e 's/(bit<16>[[:space:]]+etherType;)/\\1\\n    \\1/g' "
            f"-e 's/(bit<16>[[:space:]]+ether_type;)/\\1\\n    \\1/g' "
            f"{p4_name}.p4 ",
        )
        self.runtime.exec(params.host_name, "pkill -f simple_switch")
        self.runtime.exec(params.host_name, f"./hostlab/{params.host_name}.startup")

    def verify_fault(self, params: P4HeaderDefinitionErrorParams) -> dict:
        """Verify the P4 JSON is missing (compilation failed) or switch is not running."""
        p4_name = (
            params.p4_name
            if params.p4_name is not None
            else self.runtime.exec(params.host_name, "echo *.p4 | sed 's/\\.p4//'")
        )
        json_check = self.runtime.exec(
            params.host_name,
            f"ls {p4_name}.json 2>/dev/null && echo exists || echo missing",
        ).strip()
        switch_check = self.runtime.exec(
            params.host_name, "pgrep -a simple_switch 2>/dev/null || echo NONE"
        ).strip()
        json_exists = "exists" in json_check
        switch_running = "simple_switch" in switch_check and switch_check != "NONE"
        verified = not json_exists or not switch_running
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "params.host_name": params.host_name,
                "p4_name": p4_name,
                "json_exists": json_exists,
                "switch_running": switch_running,
            },
        )


# ==================================================================
# Problem: P4 compilation error due to parser state issue
# ==================================================================


class P4CompilationErrorParserStateParams(BaseModel):
    """Parameters for injecting a P4 parser state compilation error fault."""

    host_name: str = Field(description="Target BMv2 switch name.")
    p4_name: Optional[str] = Field(
        default=None,
        description="P4 program name (without suffix). Defaults to runtime detection.",
    )


class P4CompilationErrorParserState(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.SOFTWARE
    symptom = FailureSymptom.DOWN
    scope = FailureScope.NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "p4_compilation_error_parser_state"
    TAGS: str = ["p4"]

    Params = P4CompilationErrorParserStateParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: P4CompilationErrorParserStateParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4CompilationErrorParserStateParams):
        p4_name = (
            params.p4_name
            if params.p4_name is not None
            else self.runtime.exec(params.host_name, "echo *.p4 | sed 's/\\.p4//'")
        )
        self.runtime.exec(
            params.host_name,
            f"cp {p4_name}.p4 {p4_name}.p4.bak && "
            f"rm {p4_name}.json && "
            f"sed -Ei 's/state /states /g' {p4_name}.p4 ",
        )
        self.runtime.exec(params.host_name, "pkill -f simple_switch")
        self.runtime.exec(params.host_name, f"./hostlab/{params.host_name}.startup")

    def verify_fault(self, params: P4CompilationErrorParserStateParams) -> dict:
        """Verify the P4 JSON is missing (compilation failed) or switch is not running."""
        p4_name = (
            params.p4_name
            if params.p4_name is not None
            else self.runtime.exec(params.host_name, "echo *.p4 | sed 's/\\.p4//'")
        )
        json_check = self.runtime.exec(
            params.host_name,
            f"ls {p4_name}.json 2>/dev/null && echo exists || echo missing",
        ).strip()
        switch_check = self.runtime.exec(
            params.host_name, "pgrep -a simple_switch 2>/dev/null || echo NONE"
        ).strip()
        json_exists = "exists" in json_check
        switch_running = "simple_switch" in switch_check and switch_check != "NONE"
        verified = not json_exists or not switch_running
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "params.host_name": params.host_name,
                "p4_name": p4_name,
                "json_exists": json_exists,
                "switch_running": switch_running,
            },
        )


# ==================================================================
# Problem: P4 table entry missing
# ==================================================================


class P4TableEntryMissingParams(BaseModel):
    """Parameters for injecting a P4 table entry missing fault."""

    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableEntryMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4_table_entry_missing"
    TAGS: str = ["p4"]

    Params = P4TableEntryMissingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._cleared_table: str | None = None

    def root_cause_resources(self, params: P4TableEntryMissingParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TableEntryMissingParams):
        table_name = _find_table_with_entries(self.runtime, params.host_name)
        _cli_run(self.runtime, params.host_name, f"table_clear {table_name}")
        self._cleared_table = table_name
        logger.info(
            f"Injected fault: Deleted table entries on {params.host_name} ({table_name})"
        )

    def verify_fault(self, params: P4TableEntryMissingParams) -> dict:
        """Verify the forwarding table has no match entries."""
        table_name = self._cleared_table or _find_table_with_entries(
            self.runtime, params.host_name
        )
        table_dump = _cli_run(
            self.runtime, params.host_name, f"table_dump {table_name}"
        ).strip()
        verified = (
            "0 entries" in table_dump
            or table_dump == ""
            or "Dumping entry" not in table_dump
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "params.host_name": params.host_name,
                "table_name": table_name,
                "table_dump": table_dump,
            },
        )


# ==================================================================
# Problem: P4 table entry misconfig
# ==================================================================


class P4TableEntryMisconfigParams(BaseModel):
    """Parameters for injecting a P4 table entry misconfiguration fault."""

    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableEntryMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.MISROUTING
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4_table_entry_misconfig"
    TAGS: str = ["p4"]

    Params = P4TableEntryMisconfigParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._misconfig_details: dict | None = None

    def root_cause_resources(self, params: P4TableEntryMisconfigParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TableEntryMisconfigParams):
        self._misconfig_details = _misconfigure_first_table_entry(
            self.runtime, params.host_name
        )
        logger.info(
            f"Injected fault: Misconfigured table entry on {params.host_name} "
            f"({self._misconfig_details['table_name']} handle {self._misconfig_details['entry_handle']})"
        )

    def verify_fault(self, params: P4TableEntryMisconfigParams) -> dict:
        """Verify a table entry action was modified via simple_switch_CLI."""
        if self._misconfig_details:
            table_name = self._misconfig_details["table_name"]
            handle = self._misconfig_details["entry_handle"]
            dump = _cli_run(
                self.runtime,
                params.host_name,
                f"table_dump_entry {table_name} {handle}",
            )
            verified = _entry_matches_misconfig(dump, self._misconfig_details)
        else:
            verified, table_name = _detect_misconfigured_entry(
                self.runtime, params.host_name
            )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "params.host_name": params.host_name,
                "table_name": table_name
                or (self._misconfig_details or {}).get("table_name"),
                "misconfig_details": self._misconfig_details,
            },
        )


# ==================================================================
# Problem: MPLS Label Limit Exceeded
# ==================================================================


class P4MPLSLabelLimitExceededParams(BaseModel):
    """Parameters for injecting an MPLS label limit exceeded fault."""

    host_name: str = Field(description="Target BMv2 switch name.")


class P4MPLSLabelLimitExceeded(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.RESOURCE
    symptom = FailureSymptom.LOSS
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "mpls_label_limit_exceeded"
    TAGS: str = ["mpls"]

    Params = P4MPLSLabelLimitExceededParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: P4MPLSLabelLimitExceededParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4MPLSLabelLimitExceededParams):
        self.runtime.exec(
            params.host_name,
            "cp mpls.p4 mpls.p4.bak && "
            "rm mpls.json && "
            "sed -Ei 's/#define[[:space:]]+CONST_MAX_LABELS[[:space:]]+10/#define CONST_MAX_LABELS 2/g' mpls.p4 ",
        )
        self.runtime.exec(params.host_name, "pkill -f simple_switch")
        self.runtime.exec(params.host_name, f"./hostlab/{params.host_name}.startup")
        logger.info(
            f"Injected MPLS label limit exceeded fault on device: {params.host_name}"
        )

    def verify_fault(self, params: P4MPLSLabelLimitExceededParams) -> dict:
        """Verify CONST_MAX_LABELS was changed to 2 and the JSON may be missing."""
        const_check = self.runtime.exec(
            params.host_name,
            "grep 'CONST_MAX_LABELS 2' mpls.p4 2>/dev/null && echo found || echo absent",
        ).strip()
        json_check = self.runtime.exec(
            params.host_name, "ls mpls.json 2>/dev/null && echo exists || echo missing"
        ).strip()
        const_modified = "found" in const_check
        json_exists = "exists" in json_check
        verified = const_modified
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "params.host_name": params.host_name,
                "const_modified": const_modified,
                "json_exists": json_exists,
            },
        )
