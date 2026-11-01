from __future__ import annotations

import time
from typing import Any, Protocol

from nika.net_env.contract import (
    AdjacencyExpectation,
    ValidationContract,
    ValidationIntent,
    ValidationReport,
    ValidationResult,
    ValidationSanityResult,
)
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.client import BatfishClient
from nika.validation.batfish.compiler import (
    BatfishQuestion,
    UnsupportedIntentError,
    compile_intent,
)
from nika.validation.batfish.service import BATFISH_IMAGE, PYBATFISH_VERSION


class BatfishQueryClient(Protocol):
    def initialize(self, snapshot: ValidationSnapshot) -> dict[str, Any]: ...

    def execute(self, question: BatfishQuestion) -> list[dict[str, Any]]: ...

    def sanity_checks(self) -> tuple: ...


class BatfishVerifier:
    name = "batfish"
    supported_properties = frozenset(
        {
            "reachability",
            "isolation",
            "waypoint",
            "adjacency",
            "bgp_route",
            "route_policy",
            "route_next_hops",
        }
    )

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9996,
        client: BatfishQueryClient | None = None,
    ) -> None:
        self._client = client or BatfishClient(host=host, port=port)

    def verify(
        self, contract: ValidationContract, snapshot: ValidationSnapshot
    ) -> ValidationReport:
        started = time.monotonic()
        try:
            initialized = self._client.initialize(snapshot)
        except Exception as exc:  # noqa: BLE001 - provider failure is report evidence
            reason = f"Batfish initialization failed: {exc}"
            results = tuple(
                ValidationResult(
                    intent=intent.id,
                    verifier=self.name,
                    status="error",
                    evidence={
                        "snapshot_id": snapshot.snapshot_id,
                        "error_type": type(exc).__name__,
                    },
                    reason=reason,
                    duration_ms=_elapsed(started),
                )
                for intent in contract.intents
            )
            return ValidationReport.from_results(
                contract,
                self.name,
                results,
                sanity=(
                    ValidationSanityResult(
                        check="provider_initialization",
                        status="error",
                        evidence={"error_type": type(exc).__name__},
                        reason=reason,
                        duration_ms=_elapsed(started),
                    ),
                ),
                metadata={
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot": snapshot.metadata,
                    "pybatfish_version": PYBATFISH_VERSION,
                    "batfish_image": BATFISH_IMAGE,
                },
            )
        try:
            sanity = self._client.sanity_checks()
        except Exception as exc:  # noqa: BLE001 - provider failure is report evidence
            sanity = (
                ValidationSanityResult(
                    check="sanity_execution",
                    status="error",
                    evidence={"error_type": type(exc).__name__},
                    reason=str(exc),
                    duration_ms=_elapsed(started),
                ),
            )
        results = tuple(
            self._verify_intent(
                intent,
                snapshot,
                initialized,
                limitation=_model_limitation(contract, intent, snapshot),
            )
            for intent in contract.intents
        )
        return ValidationReport.from_results(
            contract,
            self.name,
            results,
            sanity=sanity,
            metadata={
                "snapshot_id": snapshot.snapshot_id,
                "snapshot": snapshot.metadata,
                "batfish": initialized,
                "pybatfish_version": PYBATFISH_VERSION,
                "batfish_image": BATFISH_IMAGE,
                "semantic_oracle": "static/model-predicted",
            },
        )

    def _verify_intent(
        self,
        intent: ValidationIntent,
        snapshot: ValidationSnapshot,
        initialized: dict[str, Any],
        limitation: str | None = None,
    ) -> ValidationResult:
        started = time.monotonic()
        evidence: dict[str, Any] = {
            "expected_behavior": intent.expected,
            "expected_intent": intent.model_dump(mode="json"),
            "snapshot_id": snapshot.snapshot_id,
            "batfish_version": (initialized.get("components") or {}).get("Batfish"),
            "pybatfish_version": PYBATFISH_VERSION,
            "batfish_image": BATFISH_IMAGE,
            "semantic_oracle": "static/model-predicted",
        }
        if limitation is not None:
            evidence["model_limitation"] = limitation
            return ValidationResult(
                intent=intent.id,
                verifier=self.name,
                status="unsupported",
                supported=False,
                evidence=evidence,
                reason=limitation,
                duration_ms=_elapsed(started),
            )
        try:
            questions = compile_intent(intent)
        except UnsupportedIntentError as exc:
            return ValidationResult(
                intent=intent.id,
                verifier=self.name,
                status="unsupported",
                supported=False,
                evidence=evidence,
                reason=str(exc),
                duration_ms=_elapsed(started),
            )
        try:
            answers = [
                {
                    "purpose": question.purpose,
                    "counterexamples": self._client.execute(question),
                }
                for question in questions
            ]
            if intent.property == "adjacency":
                passed, observed, violations, matched = _evaluate_adjacency(
                    intent.adjacency, answers[0]["counterexamples"]
                )
                evidence["queries"] = [
                    {
                        "purpose": answers[0]["purpose"],
                        "model_row_count": len(answers[0]["counterexamples"]),
                        "observed_sessions": matched[:4],
                    }
                ]
            elif intent.property == "route_next_hops":
                expected = intent.route_next_hops
                relevant = [
                    row
                    for row in answers[0]["counterexamples"]
                    if _node(row.get("Node")) == expected.node
                    and str(row.get("Network")) == expected.prefix
                    and str(row.get("VRF")) == expected.vrf
                ]
                actual = {str(row.get("Next_Hop_IP")) for row in relevant}
                passed = bool(relevant) and actual == set(expected.next_hops)
                violations = (
                    []
                    if passed
                    else [
                        {
                            "observed_next_hops": sorted(actual),
                            "expected_next_hops": list(expected.next_hops),
                        }
                    ]
                )
                observed = (
                    "routing next hops match" if passed else "routing next hops differ"
                )
                evidence["queries"] = [{"observations": relevant}]
            elif intent.property in {"bgp_route", "route_policy"}:
                rows = answers[0]["counterexamples"]
                if intent.bgp_route is not None:
                    expected = intent.bgp_route
                    relevant = [
                        row
                        for row in rows
                        if _node(row.get("Node")) == expected.node
                        and str(row.get("Network")) == expected.prefix
                        and "BEST" in str(row.get("Status", "")).upper()
                    ]
                    paths = [str(row.get("AS_Path", "")).split() for row in relevant]
                    passed = bool(relevant) == expected.present and all(
                        (
                            expected.exit_asn is None
                            or path[:1] == [str(expected.exit_asn)]
                        )
                        and (
                            expected.origin_asn is None
                            or path[-1:] == [str(expected.origin_asn)]
                        )
                        for path in paths
                    )
                else:
                    relevant = rows
                    expected_action = "PERMIT" if intent.route_policy.permit else "DENY"
                    passed = bool(relevant) and all(
                        row.get("Action") == expected_action for row in relevant
                    )
                violations = (
                    []
                    if passed
                    else relevant or [{"missing_expected_route_or_policy": True}]
                )
                observed = (
                    "declared routing behavior holds"
                    if passed
                    else "routing behavior differs from the original requirement"
                )
                evidence["queries"] = [
                    {"purpose": answers[0]["purpose"], "observations": relevant}
                ]
            else:
                violations = [
                    {
                        "purpose": answer["purpose"],
                        "rows": answer["counterexamples"][:5],
                    }
                    for answer in answers
                    if answer["counterexamples"]
                ]
                passed = not violations
                observed = (
                    "no counterexample found"
                    if passed
                    else "Batfish found a flow or path that contradicts the contract"
                )
                evidence["queries"] = [
                    {
                        "purpose": answer["purpose"],
                        "counterexamples": answer["counterexamples"][:5],
                        "counterexample_count": len(answer["counterexamples"]),
                    }
                    for answer in answers
                ]
            evidence["observed_model_behavior"] = observed
            evidence["violations"] = violations
            return ValidationResult(
                intent=intent.id,
                verifier=self.name,
                status="passed" if passed else "failed",
                evidence=evidence,
                reason=None if passed else observed,
                duration_ms=_elapsed(started),
            )
        except Exception as exc:  # noqa: BLE001 - query failures are evidence
            evidence["error_type"] = type(exc).__name__
            return ValidationResult(
                intent=intent.id,
                verifier=self.name,
                status="error",
                evidence=evidence,
                reason=str(exc),
                duration_ms=_elapsed(started),
            )


def _model_limitation(
    contract: ValidationContract,
    intent: ValidationIntent,
    snapshot: ValidationSnapshot,
) -> str | None:
    if bool(contract.design_source.get("rpki")) and intent.property != "adjacency":
        return (
            "Pinned Batfish does not model FRR RPKI route-map matches; "
            "flow and path results for this snapshot are unsupported"
        )
    unmodeled = snapshot.metadata.get("unmodeled_protocols", ())
    if intent.adjacency is not None and intent.adjacency.protocol in unmodeled:
        return (
            f"The {snapshot.metadata.get('routing_model')} snapshot does not model "
            f"{intent.adjacency.protocol.upper()} adjacencies"
        )
    return None


def _evaluate_adjacency(
    expected: AdjacencyExpectation | None, rows: list[dict[str, Any]]
) -> tuple[bool, str, list[dict[str, Any]], list[dict[str, Any]]]:
    assert expected is not None
    if expected.protocol == "isis":
        relevant = [
            row
            for row in rows
            if _interface_endpoint_matches(
                row.get("Interface"), expected.local_node, expected.local_interface
            )
            and _interface_endpoint_matches(
                row.get("Remote_Interface"),
                expected.remote_node,
                expected.remote_interface,
            )
        ]
        passed = bool(relevant)
        return (
            passed,
            "expected IS-IS link is present in the modeled topology"
            if passed
            else "expected IS-IS link is absent from the modeled topology",
            []
            if passed
            else [
                row
                for row in rows
                if _node(row.get("Interface")) == expected.local_node
            ][:10],
            relevant,
        )
    if expected.protocol == "bgp":
        compatibility = [
            row
            for row in rows
            if row.get("analysis") == "compatibility"
            and _node(row.get("Node")) == expected.local_node
            and _remote_node_matches(row.get("Remote_Node"), expected.remote_node)
            and _ip_equal(row.get("Local_IP"), expected.local_address)
            and _ip_equal(row.get("Remote_IP"), expected.remote_address)
        ]
        established = [
            row
            for row in rows
            if row.get("analysis") == "establishment"
            and _node(row.get("Node")) == expected.local_node
            and _remote_node_matches(row.get("Remote_Node"), expected.remote_node)
            and _ip_equal(row.get("Local_IP"), expected.local_address)
            and _ip_equal(row.get("Remote_IP"), expected.remote_address)
        ]
        compatible = any(
            str(row.get("Configured_Status")) in {"UNIQUE_MATCH", "DYNAMIC_MATCH"}
            and _asn_equal(row.get("Local_AS"), expected.local_asn)
            and _asn_equal(row.get("Remote_AS"), expected.remote_asn)
            and _session_type_equal(row.get("Session_Type"), expected.session_type)
            for row in compatibility
        )
        modeled_up = any(
            str(row.get("Established_Status")) == "ESTABLISHED" for row in established
        )
        configured = [
            row
            for row in rows
            if row.get("analysis") == "configuration"
            and _node(row.get("Node")) == expected.local_node
            and _ip_equal(row.get("Remote_IP"), expected.remote_address)
        ]
        role_matches = expected.route_reflector_client is None or any(
            row.get("Route_Reflector_Client") == expected.route_reflector_client
            for row in configured
        )
        passed = compatible and modeled_up and role_matches
        relevant = compatibility + established + configured
        candidates = [
            row for row in rows if _node(row.get("Node")) == expected.local_node
        ][:10]
        return (
            passed,
            "expected BGP session is compatible and predicted established"
            if passed
            else "expected BGP route-reflector-client role differs from the specification"
            if not role_matches
            else "expected BGP session is absent, incompatible, or not predicted established",
            [] if passed else relevant or candidates,
            relevant,
        )

    relevant = [
        row
        for row in rows
        if _node(row.get("Interface")) == expected.local_node
        and _node(row.get("Remote_Interface")) == expected.remote_node
        and _ip_equal(row.get("IP"), expected.local_address)
        and _ip_equal(row.get("Remote_IP"), expected.remote_address)
    ]
    passed = any(
        str(row.get("Session_Status", "")).startswith("ESTABLISHED")
        and (
            expected.ospf_area is None
            or _area_equal(row.get("Area"), expected.ospf_area)
        )
        for row in relevant
    )
    return (
        passed,
        "expected OSPF session is compatible"
        if passed
        else "expected OSPF session is absent or incompatible",
        []
        if passed
        else relevant
        or [row for row in rows if _node(row.get("Interface")) == expected.local_node][
            :10
        ],
        relevant,
    )


def _node(value: Any) -> str:
    text = str(value or "")
    return text.split("[")[0]


def _interface_endpoint_matches(value: Any, node: str, interface: str) -> bool:
    text = str(value or "")
    if "[" not in text or not text.endswith("]"):
        return False
    actual_node, actual_interface = text[:-1].split("[", 1)
    return actual_node == node and actual_interface == interface


def _ip_equal(value: Any, expected: str) -> bool:
    return str(value or "").split("/")[0] == expected


def _remote_node_matches(value: Any, expected: str) -> bool:
    return value is None or _node(value) == expected


def _asn_equal(value: Any, expected: int | None) -> bool:
    if expected is None:
        return True
    if isinstance(value, list):
        return expected in value or str(expected) in {str(item) for item in value}
    return str(value) == str(expected)


def _area_equal(value: Any, expected: str) -> bool:
    from ipaddress import IPv4Address

    try:
        return int(value) == int(IPv4Address(expected))
    except (TypeError, ValueError):
        return str(value) == expected


def _session_type_equal(value: Any, expected: str | None) -> bool:
    if expected is None:
        return True
    modeled = str(value or "").upper()
    if expected == "ibgp":
        return modeled == "IBGP"
    return modeled.startswith("EBGP")


def _elapsed(started: float) -> float:
    return (time.monotonic() - started) * 1000
