"""Synthesized prefix policy terms and fixed security-policy scope."""

from __future__ import annotations

from ipaddress import IPv4Network

from pydantic import Field, StrictInt, model_validator

from nika.net_env.contract import ContractModel


class BgpPolicyTerm(ContractModel):
    prefix: str
    permit: bool
    local_preference: StrictInt | None = Field(default=None, ge=1, le=4294967295)

    @model_validator(mode="after")
    def validate_prefix(self) -> BgpPolicyTerm:
        IPv4Network(self.prefix)
        return self


def fixed_policy_prefixes(plan) -> set[str]:
    """Prefixes whose ROV/export/blackhole semantics remain in the fixed sketch."""
    prefixes = set(plan.inventory.get("leak_prefixes", ()))
    if prefix := plan.inventory.get("target_prefix"):
        prefixes.add(prefix)
    prefixes.update(p for n in plan.nodes for p in n.export_deny_prefixes)
    return prefixes
