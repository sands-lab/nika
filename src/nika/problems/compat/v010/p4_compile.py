"""Legacy P4 compile failures for the 0.1.0 benchmark cases."""

from nika.problems.base import FailureDomain
from nika.problems.compat.v010.p4_source import (
    LegacyP4SourceParams,
    _LegacyP4SourceFault,
)
from nika.problems.rca import node_resource


class P4CompilationErrorParserState(_LegacyP4SourceFault):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "p4_compilation_error_parser_state"
    description = "An invalid P4 parser state prevents a switch rollout."
    TAGS = ["p4"]
    COMPATIBLE_COLUMNS = frozenset({"p4_bloom_filter", "p4_counter"})
    old_text = "state "
    new_text = "states "

    def root_cause_resources(self, params: LegacyP4SourceParams):
        return [node_resource(params.host_name)]


class P4HeaderDefinitionError(_LegacyP4SourceFault):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "p4_header_definition_error"
    description = "An invalid P4 header definition prevents a switch rollout."
    TAGS = ["p4"]
    COMPATIBLE_COLUMNS = frozenset({"p4_bloom_filter", "p4_counter"})
    old_text = "bit<16> etherType;"
    old_pattern = "bit<16>[[:space:]]*etherType;"
    new_text = "bit<16> etherType; bit<16> etherType;"

    def root_cause_resources(self, params: LegacyP4SourceParams):
        return [node_resource(params.host_name)]
