from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    fabric_misconfig_group_id,
    fabric_table_entry_prefix,
    load_intent,
    run_manager,
)
from nika.utils.logger import system_logger

logger = system_logger


class P4TableEntryMissingParams(BaseModel):
    """Parameters for removing a P4Runtime forwarding entry."""

    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableEntryMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_table_entry_missing"
    description = "A required P4 forwarding table entry is missing."
    TAGS: str = ["p4", "p4_runtime"]
    Params = P4TableEntryMissingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._cleared_prefix: str | None = None

    def root_cause_resources(self, params: P4TableEntryMissingParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TableEntryMissingParams):
        import time

        intent = load_intent(self.runtime)
        prefix = fabric_table_entry_prefix(intent, params.host_name, self.scenario_name)
        if not prefix:
            raise RuntimeError(f"No IPv4 LPM prefix on {params.host_name}")
        result = run_manager(
            self.runtime,
            "delete-lpm",
            "--switch",
            params.host_name,
            "--prefix",
            prefix,
        )
        if not result.get("ok") or result.get("present"):
            raise RuntimeError(
                f"delete-lpm failed for {prefix} on {params.host_name}: {result}"
            )
        self._cleared_prefix = prefix
        # Brief settle so the data plane drops before symptom probes.
        time.sleep(1.0)
        logger.info(
            "Injected fault: deleted P4Runtime LPM %s on %s (%s)",
            prefix,
            params.host_name,
            result,
        )

    def verify_fault(self, params: P4TableEntryMissingParams) -> dict:
        """Verify that the selected live P4Runtime entry is absent."""
        intent = load_intent(self.runtime)
        prefix = self._cleared_prefix or fabric_table_entry_prefix(
            intent, params.host_name, self.scenario_name
        )
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        switch_state = (observed.get("switches") or {}).get(params.host_name) or {}
        entries = switch_state.get("ipv4_lpm") or []
        # Require a successful read that still shows other routes; an empty
        # switch view would false-verify "prefix absent" on a failed read.
        readable = bool(switch_state) and len(entries) >= 1
        present = any(entry.get("prefix") == prefix for entry in entries)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=readable and not present,
            details={
                "params.host_name": params.host_name,
                "prefix": prefix,
                "present": present,
                "readable": readable,
                "ipv4_lpm": entries,
            },
        )


class P4TableEntryMisconfigParams(BaseModel):
    """Parameters for changing a P4Runtime forwarding entry."""

    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableEntryMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_table_entry_misconfig"
    description = "A P4 forwarding table entry is misconfigured."
    TAGS: str = ["p4", "p4_runtime"]
    Params = P4TableEntryMisconfigParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._misconfig_details: dict | None = None

    def root_cause_resources(self, params: P4TableEntryMisconfigParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TableEntryMisconfigParams):
        intent = load_intent(self.runtime)
        prefix = fabric_table_entry_prefix(intent, params.host_name, self.scenario_name)
        if prefix is None:
            raise RuntimeError(f"No IPv4 LPM prefix on {params.host_name}")
        other = fabric_misconfig_group_id(
            intent, params.host_name, prefix, self.scenario_name
        )
        if other is None:
            raise RuntimeError(
                f"No alternate ActionSelector group on {params.host_name}"
            )
        result = run_manager(
            self.runtime,
            "modify-lpm-group",
            "--switch",
            params.host_name,
            "--prefix",
            prefix,
            "--group-id",
            str(other),
        )
        self._misconfig_details = {
            "prefix": prefix,
            "group_id": int(other),
            "result": result,
        }
        logger.info(
            "Injected fault: misconfigured P4Runtime LPM %s on %s -> group %s",
            prefix,
            params.host_name,
            other,
        )

    def verify_fault(self, params: P4TableEntryMisconfigParams) -> dict:
        """Verify that the selected live P4Runtime entry uses the wrong group."""
        intent = load_intent(self.runtime)
        details = self._misconfig_details or {}
        prefix = details.get("prefix") or fabric_table_entry_prefix(
            intent, params.host_name, self.scenario_name
        )
        group_id = details.get("group_id")
        if group_id is None and prefix:
            group_id = fabric_misconfig_group_id(
                intent, params.host_name, prefix, self.scenario_name
            )
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        entries = (observed.get("switches") or {}).get(params.host_name, {}).get(
            "ipv4_lpm"
        ) or []
        got = next((entry for entry in entries if entry.get("prefix") == prefix), None)
        verified = (
            got is not None
            and prefix is not None
            and group_id is not None
            and int(got.get("group_id") or -1) == int(group_id)
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "params.host_name": params.host_name,
                "prefix": prefix,
                "expected_group_id": group_id,
                "observed": got,
            },
        )
