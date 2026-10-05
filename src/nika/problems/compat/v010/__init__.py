"""Failure implementations used only by nika-bench 0.1.0 cases.

The failure registry skips this package. ``FAILURES`` holds 0.1.0 root causes
absent from the current registry. ``SCENARIO_FAILURE_OVERRIDES`` holds 0.1.0
implementations of a current failure ID that apply only on an original 0.1.0
lab.
"""

from __future__ import annotations

from nika.problems.compat.v010.endpoint import HostCrash, SenderApplicationDelay
from nika.problems.compat.v010.p4_compile import (
    P4CompilationErrorParserState,
    P4HeaderDefinitionError,
)
from nika.problems.compat.v010.p4_source import (
    P4AggressiveDetectionThresholds,
    P4MPLSLabelLimitExceeded,
)
from nika.problems.compat.v010.p4_table import (
    P4TableEntryMisconfig,
    P4TableEntryMissing,
)
from nika.problems.compat.v010.routing import BGPBlackholeRouteLeak
from nika.problems.compat.v010.sdn import (
    FlowRuleLoop,
    FlowRuleShadowing,
    SDNControllerCrash,
    SouthboundPortBlock,
    SouthboundPortMismatch,
)
from nika.problems.compat.v010.traffic import LinkBandwidthThrottling
from nika.problems.compat.v010.vpn import VPNMembershipMissing
from nika.problems.base import ProblemBase

RELEASE_VERSION = "0.1.0"

FAILURES: dict[str, type[ProblemBase]] = {
    cls.root_cause_name: cls
    for cls in (
        BGPBlackholeRouteLeak,
        HostCrash,
        LinkBandwidthThrottling,
        P4AggressiveDetectionThresholds,
        P4CompilationErrorParserState,
        P4HeaderDefinitionError,
        P4MPLSLabelLimitExceeded,
        SenderApplicationDelay,
    )
}

_POX_FAULTS = (
    FlowRuleLoop,
    FlowRuleShadowing,
    SDNControllerCrash,
    SouthboundPortBlock,
    SouthboundPortMismatch,
)
_P4_TABLE_FAULTS = (P4TableEntryMisconfig, P4TableEntryMissing)

SCENARIO_FAILURE_OVERRIDES: dict[tuple[str, str], type[ProblemBase]] = {
    **{
        (scenario, cls.root_cause_name): cls
        for scenario in ("sdn_clos", "sdn_star")
        for cls in _POX_FAULTS
    },
    **{
        (scenario, cls.root_cause_name): cls
        for scenario in ("p4_bloom_filter", "p4_counter")
        for cls in _P4_TABLE_FAULTS
    },
    ("rip_small_internet_vpn", VPNMembershipMissing.root_cause_name): (
        VPNMembershipMissing
    ),
}


def legacy_010_fault_types() -> set[str]:
    """0.1.0 fault IDs offered to 0.1.0 sessions beyond the registry IDs."""
    return set(FAILURES) | {name for _, name in SCENARIO_FAILURE_OVERRIDES}
