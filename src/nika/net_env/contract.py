"""Backend-independent network validation contracts and results."""

from __future__ import annotations

import json
from collections.abc import Iterable
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PropertyType = Literal["reachability", "isolation", "waypoint", "adjacency"]
IntentLevel = Literal["required", "optional"]
ResultStatus = Literal["passed", "failed", "skipped", "unsupported", "error"]

VALIDATION_CONTRACT_FILENAME = "validation-contract.json"
VALIDATION_RESULTS_FILENAME = "validation-results.json"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkEntity(ContractModel):
    """A concrete semantic object resolved before a verifier runs."""

    kind: Literal["node", "endpoint", "prefix", "service"]
    name: str = Field(min_length=1)
    address: str | None = None
    node: str | None = None

    @model_validator(mode="after")
    def validate_address(self) -> "NetworkEntity":
        if self.kind == "prefix":
            if self.address is None:
                raise ValueError("prefix entities require an IPv4 prefix")
            IPv4Network(self.address)
        elif self.address is not None:
            IPv4Address(self.address)
        return self


class EntitySelector(ContractModel):
    """A scenario-build-time selector; selectors are absent from final intents."""

    kind: Literal["entity", "group"]
    value: str = Field(min_length=1)


class SelectorCatalog(ContractModel):
    entities: tuple[NetworkEntity, ...]
    groups: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> "SelectorCatalog":
        names = [entity.name for entity in self.entities]
        if len(names) != len(set(names)):
            raise ValueError("selector catalog entity names must be unique")
        known = set(names)
        for group, members in self.groups.items():
            missing = set(members) - known
            if missing:
                raise ValueError(
                    f"group {group!r} references unknown entities: {sorted(missing)}"
                )
        return self

    def expand(self, selector: EntitySelector) -> tuple[NetworkEntity, ...]:
        by_name = {entity.name: entity for entity in self.entities}
        names = (
            (selector.value,)
            if selector.kind == "entity"
            else self.groups.get(selector.value)
        )
        if names is None:
            raise ValueError(f"unknown selector group {selector.value!r}")
        missing = set(names) - set(by_name)
        if missing:
            raise ValueError(f"selector references unknown entities: {sorted(missing)}")
        return tuple(by_name[name] for name in sorted(set(names)))


class TrafficSelector(ContractModel):
    ip_version: Literal[4] = 4
    protocol: Literal["ipv4", "icmp", "tcp", "udp"] = "ipv4"
    destination_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def validate_port(self) -> "TrafficSelector":
        if self.protocol in {"tcp", "udp"} and self.destination_port is None:
            raise ValueError("TCP and UDP selectors require destination_port")
        if self.destination_port is not None and self.protocol not in {"tcp", "udp"}:
            raise ValueError("destination_port is only valid for TCP or UDP")
        return self


class PathConstraint(ContractModel):
    must_traverse: tuple[str, ...] = ()
    must_avoid: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_nodes(self) -> "PathConstraint":
        overlap = set(self.must_traverse) & set(self.must_avoid)
        if overlap:
            raise ValueError(
                f"path nodes cannot be both required and forbidden: {sorted(overlap)}"
            )
        if not self.must_traverse and not self.must_avoid:
            raise ValueError("path constraint must require or forbid at least one node")
        return self


class AdjacencyExpectation(ContractModel):
    protocol: Literal["bgp", "ospf"]
    local_node: str
    remote_node: str
    local_address: str
    remote_address: str
    local_asn: int | None = Field(default=None, ge=1, le=4_294_967_295)
    remote_asn: int | None = Field(default=None, ge=1, le=4_294_967_295)
    update_source: str | None = None
    session_type: Literal["ibgp", "ebgp"] | None = None
    ospf_area: str | None = None
    local_router_id: str | None = None
    remote_router_id: str | None = None

    @model_validator(mode="after")
    def validate_protocol_fields(self) -> "AdjacencyExpectation":
        if self.protocol == "bgp":
            if self.local_asn is None or self.remote_asn is None:
                raise ValueError("BGP adjacency requires local_asn and remote_asn")
            if self.session_type is None:
                raise ValueError("BGP adjacency requires session_type")
            if self.ospf_area is not None:
                raise ValueError("BGP adjacency cannot define ospf_area")
        elif any(
            value is not None
            for value in (
                self.local_asn,
                self.remote_asn,
                self.update_source,
                self.session_type,
            )
        ):
            raise ValueError("OSPF adjacency cannot define BGP fields")
        if self.protocol == "ospf":
            if self.local_router_id is None or self.remote_router_id is None:
                raise ValueError("OSPF adjacency requires local and remote router IDs")
            IPv4Address(self.local_router_id)
            IPv4Address(self.remote_router_id)
        return self


class ValidationIntent(ContractModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str = Field(min_length=1)
    property: PropertyType
    expected: Literal["reachable", "unreachable", "path_compliant", "established"]
    level: IntentLevel = "required"
    source: NetworkEntity | None = None
    destination: NetworkEntity | None = None
    traffic: TrafficSelector | None = None
    path: PathConstraint | None = None
    adjacency: AdjacencyExpectation | None = None

    @model_validator(mode="after")
    def validate_property_shape(self) -> "ValidationIntent":
        expected_by_property = {
            "reachability": "reachable",
            "isolation": "unreachable",
            "waypoint": "path_compliant",
            "adjacency": "established",
        }
        if self.expected != expected_by_property[self.property]:
            raise ValueError(f"expected state does not match {self.property}")
        if self.property == "adjacency":
            if self.adjacency is None or any(
                value is not None
                for value in (self.source, self.destination, self.traffic, self.path)
            ):
                raise ValueError("adjacency intents require only adjacency")
        else:
            if self.source is None or self.destination is None or self.traffic is None:
                raise ValueError(
                    f"{self.property} requires source, destination, and traffic"
                )
            if self.adjacency is not None:
                raise ValueError(f"{self.property} cannot contain adjacency")
            if (self.property == "waypoint") != (self.path is not None):
                raise ValueError("path is required only for waypoint intents")
        return self


class ValidationContract(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    contract_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    design_source: dict[str, Any]
    intents: tuple[ValidationIntent, ...]

    @model_validator(mode="after")
    def validate_intent_ids(self) -> "ValidationContract":
        ids = [intent.id for intent in self.intents]
        if len(ids) != len(set(ids)):
            raise ValueError("intent IDs must be unique")
        if ids != sorted(ids):
            raise ValueError("intents must be sorted by stable ID")
        return self

    def to_json(self) -> str:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ValidationContract":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class ValidationResult(ContractModel):
    intent: str
    verifier: str
    status: ResultStatus
    supported: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    duration_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_support_status(self) -> "ValidationResult":
        if self.supported == (self.status == "unsupported"):
            raise ValueError("unsupported status must match supported=false")
        return self


class CoverageCounts(ContractModel):
    total: int = Field(ge=0)
    supported: int = Field(ge=0)
    validated: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    errors: int = Field(ge=0)


class ValidationCoverage(CoverageCounts):
    by_property: dict[str, CoverageCounts] = Field(default_factory=dict)
    by_adjacency_protocol: dict[str, CoverageCounts] = Field(default_factory=dict)


class ValidationSanityResult(ContractModel):
    check: str
    status: Literal["passed", "failed", "error"]
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    duration_ms: float = Field(ge=0)


class ValidationReport(ContractModel):
    contract_id: str
    verifier: str
    status: ResultStatus
    results: tuple[ValidationResult, ...]
    coverage: ValidationCoverage | None = None
    sanity: tuple[ValidationSanityResult, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_results(
        cls,
        contract: ValidationContract,
        verifier: str,
        results: Iterable[ValidationResult],
        *,
        sanity: Iterable[ValidationSanityResult] = (),
        metadata: dict[str, Any] | None = None,
    ) -> "ValidationReport":
        rows = tuple(results)
        sanity_rows = tuple(sanity)
        levels = {intent.id: intent.level for intent in contract.intents}
        result_ids = [row.intent for row in rows]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("validation results must contain one row per intent")
        if set(result_ids) != set(levels):
            missing = sorted(set(levels) - set(result_ids))
            unknown = sorted(set(result_ids) - set(levels))
            raise ValueError(
                f"validation result IDs do not match contract; missing={missing}, unknown={unknown}"
            )
        if any(row.verifier != verifier for row in rows):
            raise ValueError(
                "validation result verifier does not match report verifier"
            )
        required = [row for row in rows if levels.get(row.intent) == "required"]
        if any(row.status == "error" for row in required) or any(
            row.status == "error" for row in sanity_rows
        ):
            status: ResultStatus = "error"
        elif any(row.status == "failed" for row in required) or any(
            row.status == "failed" for row in sanity_rows
        ):
            status = "failed"
        elif any(row.status == "skipped" for row in required):
            status = "skipped"
        else:
            status = "passed"
        return cls(
            contract_id=contract.contract_id,
            verifier=verifier,
            status=status,
            results=rows,
            coverage=_coverage(contract, rows),
            sanity=sanity_rows,
            metadata=metadata or {},
        )

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ValidationReport":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _coverage(
    contract: ValidationContract, rows: tuple[ValidationResult, ...]
) -> ValidationCoverage:
    property_by_id = {intent.id: intent.property for intent in contract.intents}

    def counts(selected: tuple[ValidationResult, ...]) -> CoverageCounts:
        return CoverageCounts(
            total=len(selected),
            supported=sum(row.supported for row in selected),
            validated=sum(
                row.status not in {"unsupported", "skipped"} for row in selected
            ),
            passed=sum(row.status == "passed" for row in selected),
            failed=sum(row.status == "failed" for row in selected),
            unsupported=sum(row.status == "unsupported" for row in selected),
            errors=sum(row.status == "error" for row in selected),
        )

    by_property = {
        property_name: counts(
            tuple(row for row in rows if property_by_id[row.intent] == property_name)
        )
        for property_name in sorted(set(property_by_id.values()))
    }
    adjacency_protocol_by_id = {
        intent.id: intent.adjacency.protocol
        for intent in contract.intents
        if intent.adjacency is not None
    }
    by_adjacency_protocol = {
        protocol: counts(
            tuple(
                row
                for row in rows
                if adjacency_protocol_by_id.get(row.intent) == protocol
            )
        )
        for protocol in sorted(set(adjacency_protocol_by_id.values()))
    }
    overall = counts(rows)
    return ValidationCoverage(
        **overall.model_dump(),
        by_property=by_property,
        by_adjacency_protocol=by_adjacency_protocol,
    )
