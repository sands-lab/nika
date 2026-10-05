"""P4 source faults retained for the 0.1.0 benchmark cases."""

from __future__ import annotations

import shlex

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource


class LegacyP4SourceParams(BaseModel):
    host_name: str = Field(description="BMv2 switch with the affected P4 program.")


class _LegacyP4SourceFault(ProblemBase):
    """Apply one source change inside an isolated legacy BMv2 switch."""

    Params = LegacyP4SourceParams
    source_name: str = ""
    old_text: str = ""
    old_pattern: str = ""
    new_text: str = ""

    def _source_name(self, host: str) -> str:
        if self.source_name:
            return f"/{self.source_name}"
        program = self.runtime.exec(host, "printf '%s' /*.p4").strip()
        if " " in program or not program.endswith(".p4"):
            raise RuntimeError(f"Expected one P4 program on {host}: {program!r}")
        return program

    def _is_edited(self, host: str, source: str) -> bool:
        if self.new_text.startswith("#define"):
            result = self.runtime.exec(
                host,
                f"grep -Fxq {shlex.quote(self.new_text)} {source} && echo EDITED || true",
            )
            return "EDITED" in result
        return self.runtime.file_contains(host, source, self.new_text)

    def inject_fault(self, params: LegacyP4SourceParams):
        host = params.host_name
        source = self._source_name(host)
        backup = f"/tmp/nika-010-{self.root_cause_name}.p4"
        old = (self.old_pattern or self.old_text).replace("'", "'\\''")
        new = self.new_text.replace("'", "'\\''")
        changed = self.runtime.exec(
            host,
            f"cp {source} {backup} && sed -i 's/{old}/{new}/g' {source} && "
            "echo CHANGED",
        )
        if "CHANGED" not in changed or not self._is_edited(host, source):
            raise RuntimeError(f"P4 source edit failed on {host}: {changed}")
        self.runtime.exec(host, "pkill -x simple_switch || true")
        if self.new_text.startswith("#define"):
            self.runtime.exec(host, f"cd / && bash /hostlab/{host}.startup", timeout=90)
        else:
            # A failed compiler invocation during rollout leaves this switch down.
            result = self.runtime.exec(
                host,
                f"p4c {source} -o /tmp/nika-010-invalid.json "
                "2>&1 && echo COMPILED || echo COMPILE_FAILED",
                timeout=60,
            )
            if "COMPILE_FAILED" not in result:
                raise RuntimeError(
                    f"Expected P4 compilation failure on {host}: {result}"
                )

    def verify_fault(self, params: LegacyP4SourceParams) -> dict:
        host = params.host_name
        source = self._source_name(host)
        edited = self._is_edited(host, source)
        process = self.runtime.exec(host, "pgrep -x simple_switch || true").strip()
        running = bool(process)
        verified = edited and (
            running if self.new_text.startswith("#define") else not running
        )
        return build_verify_result(
            self.root_cause_name,
            verified,
            {"host": host, "edited": edited, "switch_running": running},
        )

    def recover_fault(self, params: LegacyP4SourceParams) -> dict:
        host = params.host_name
        source = self._source_name(host)
        backup = f"/tmp/nika-010-{self.root_cause_name}.p4"
        self.runtime.exec(
            host,
            f"pkill -x simple_switch || true; cp {backup} {source} && "
            f"cd / && bash /hostlab/{host}.startup",
            timeout=90,
        )
        edited = self._is_edited(host, source)
        running = bool(
            self.runtime.exec(host, "pgrep -x simple_switch || true").strip()
        )
        return build_verify_result(
            self.root_cause_name,
            not edited and running,
            {"host": host, "edited": edited, "switch_running": running},
        )


class P4AggressiveDetectionThresholds(_LegacyP4SourceFault):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_aggressive_detection_thresholds"
    description = "A Bloom filter drops packets at an excessively low threshold."
    TAGS = ["p4", "bloom_filter"]
    COMPATIBLE_COLUMNS = frozenset({"p4_bloom_filter"})
    source_name = "bloom_filter.p4"
    old_text = "#define PACKET_THRESHOLD 1000"
    new_text = "#define PACKET_THRESHOLD 100"

    def root_cause_resources(self, params: LegacyP4SourceParams):
        return [node_resource(params.host_name)]


class P4MPLSLabelLimitExceeded(_LegacyP4SourceFault):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "mpls_label_limit_exceeded"
    description = "The MPLS label table is limited below its required size."
    TAGS = ["p4", "mpls"]
    COMPATIBLE_COLUMNS = frozenset({"p4_mpls"})
    source_name = "mpls.p4"
    old_text = "#define CONST_MAX_LABELS 10"
    old_pattern = "#define CONST_MAX_LABELS[[:space:]]*10"
    new_text = "#define CONST_MAX_LABELS 2"

    def root_cause_resources(self, params: LegacyP4SourceParams):
        return [node_resource(params.host_name)]
