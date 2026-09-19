"""CI verification depth helpers.

Nightly on shared runners uses artifact/boot checks (session ready, inject
``verify_fault``, ground truth) rather than deep behavioral symptom probes.
Local defaults keep full evaluate_scenario / symptom / recover coverage.
"""

from __future__ import annotations

import os

_ARTIFACT_VALUES = frozenset({"artifact", "light", "1", "true", "yes"})


def artifact_verify_only() -> bool:
    """True when CI requests boot/artifact checks without deep symptom verify."""
    return os.environ.get("NIKA_CI_VERIFY_DEPTH", "").strip().lower() in _ARTIFACT_VALUES
