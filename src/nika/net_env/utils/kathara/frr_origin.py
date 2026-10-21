"""Interface for addresses that only BGP originates on Kathara FRR routers.

``lo`` usually runs the IGP, and IS-IS advertises every address on it. A
prefix configured there would leak into the IGP, which then outranks iBGP.
Like an extra router loopback kept outside the IGP, this dummy interface keeps
BGP the only protocol that carries the prefix, whichever IGP the lab runs.
"""

from __future__ import annotations

BGP_ORIGIN_IFACE = "lo1"

# Idempotent: originators already have it, injected faults may add it later.
ENSURE_BGP_ORIGIN_IFACE_CMD = (
    f"ip link show {BGP_ORIGIN_IFACE} >/dev/null 2>&1 "
    f"|| ip link add {BGP_ORIGIN_IFACE} type dummy; "
    f"ip link set {BGP_ORIGIN_IFACE} up"
)
