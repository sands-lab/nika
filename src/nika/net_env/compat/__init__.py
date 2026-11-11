"""Environment bindings for published benchmark releases."""

from __future__ import annotations


def release_environment_class(scenario: str, release: str | None, default):
    if release not in {"0.1.0", "0.2.0"}:
        return default
    if scenario == "dc_clos":
        from .published_clos import DCClos

        return DCClos
    if scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
        from .published_fabric import P4DcFabric, SDNL3Clos

        return {"sdn_l3_clos": SDNL3Clos, "p4_dc_fabric": P4DcFabric}[scenario]
    return default
