"""Phase timers for NIKA lab startup, loaded in every Python process when NIKA_PHASE_LOG is set.

Put this directory on PYTHONPATH and set NIKA_PHASE_LOG=/path/phases.jsonl. NIKA itself is
not modified: this wraps the functions on the deploy path and appends one JSON line per
phase. After start_net_env returns, it also reads, without polling during the deploy:

* every machine container's start time (docker inspect),
* every FRR router's BGP session establishment and last route update times,
* for k3s labs, node and pod Ready transition times,

so routing and cluster convergence can be placed on the same host clock.
"""

import os

if os.environ.get("NIKA_PHASE_LOG"):
    try:
        import nika_phase_probe  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - never break NIKA
        import sys

        print(f"[phase-probe] disabled: {exc!r}", file=sys.stderr)
