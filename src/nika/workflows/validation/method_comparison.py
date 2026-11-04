"""Compare three checks of the same requirement in each network state.

For every runtime check point of a scenario experiment, this records, per
original intent: the Batfish result on the analyzed configurations of that
state, a NetKAT check of the FIB that the running routers installed, and the
native probe result. Disagreements are listed for diagnosis; they are not
classified automatically, because explaining one requires the evidence of all
three checks.
"""

from __future__ import annotations

import json
from pathlib import Path

from nika.net_env.contract import (
    TrafficSelector,
    ValidationContract,
    ValidationReport,
)
from nika.runtime.spec import NodeRole

# Intents that a packet forwarding model can decide.
PACKET_PROPERTIES = frozenset({"reachability", "isolation"})


def packet_contract(contract: ValidationContract) -> ValidationContract:
    """The contract's endpoint-to-endpoint intents as ICMP predicates.

    Reachability is a round trip, as the native ping probes it; isolation is
    one-way.
    """
    intents = tuple(
        intent.model_copy(
            update={
                "traffic": TrafficSelector(
                    protocol="icmp", round_trip=intent.property == "reachability"
                )
            }
        )
        for intent in contract.intents
        if intent.property in PACKET_PROPERTIES
        and intent.source is not None
        and intent.destination is not None
        and intent.source.address
        and intent.destination.address
    )
    return contract.model_copy(update={"intents": intents})


def local_name(router: str) -> str:
    return f"{router}:local"


def _local_endpoints(contract, routers):
    """Address router endpoints of intents as the routers' local endpoints."""

    def rename(endpoint):
        if endpoint.name not in routers:
            return endpoint
        return endpoint.model_copy(
            update={"name": local_name(endpoint.name), "node": None}
        )

    return contract.model_copy(
        update={
            "intents": tuple(
                i.model_copy(
                    update={
                        "source": rename(i.source),
                        "destination": rename(i.destination),
                    }
                )
                for i in contract.intents
            )
        }
    )


def _statuses(report: ValidationReport | None) -> dict[str, str]:
    if report is None:
        return {}
    return {row.intent: row.status for row in report.results}


class MethodComparison:
    """Record the three checks at each runtime check point of one experiment."""

    def __init__(self, env, backend, output: Path):
        self.env = env
        self.backend = backend
        self.root = output / "method-comparison"
        self.root.mkdir(parents=True, exist_ok=True)
        self.summary: dict[str, dict] = {}
        native = set(getattr(env, "native_routers", {}))
        routers = {
            name
            for name, identity in env.machine_identities.items()
            if identity.role is NodeRole.ROUTER
        }
        self.routers = routers - native
        self.limit = None
        if env.backend != "kathara":
            self.limit = "The FIB check reads Linux routers on Kathara"
        elif native:
            self.limit = "The FIB check does not read native vendor routers"

    def _fib(self, stage: str, contract: ValidationContract) -> ValidationReport:
        from nika.validation.netkat.linux_fib import fib_network
        from nika.validation.netkat.verifier import NetKatVerifier, write_snapshot

        hosts, local = {}, {}
        for intent in contract.intents:
            for endpoint in (intent.source, intent.destination):
                if endpoint.name in self.routers:
                    # The router's own traffic, as an endpoint attached to it.
                    hosts[local_name(endpoint.name)] = endpoint.address
                    local[local_name(endpoint.name)] = endpoint.name
                elif endpoint.name in self.env.machine_identities:
                    hosts[endpoint.name] = endpoint.address
        state = {}
        for node in sorted(self.routers | (set(hosts) - set(local))):
            state[node] = {
                key: json.loads(self.backend.exec(node, command) or "[]")
                for key, command in (
                    ("addr", "ip -j -4 addr show"),
                    ("route", "ip -j -4 route show table main"),
                )
            }
        (self.root / f"{stage}-fib.json").write_text(json.dumps(state, indent=2))
        network = fib_network(state, self.routers, hosts, local)
        snapshot = write_snapshot(network, self.root / f"{stage}-netkat")
        return NetKatVerifier().verify(
            _local_endpoints(contract, self.routers), snapshot
        )

    def observe(
        self,
        stage: str,
        contract: ValidationContract,
        probes: list[ValidationReport],
        static: Path | None = None,
    ) -> None:
        packets = packet_contract(contract)
        fib, error = None, None
        if self.limit is None and packets.intents:
            # A FIB the adapter cannot model is recorded; it does not abort the
            # validation run that this comparison observes.
            try:
                fib = self._fib(stage, packets)
                fib.write(self.root / f"{stage}-netkat.json")
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
        static_status = {}
        if static is not None and static.exists():
            static_status = _statuses(
                ValidationReport.model_validate_json(static.read_text())
            )
        probe_status = {}
        for report in probes:
            probe_status.update(_statuses(report))
        fib_status = _statuses(fib)
        rows = {}
        for intent in packets.intents:
            row = {
                "static": static_status.get(intent.id),
                "fib": fib_status.get(
                    intent.id, "error" if error else self.limit and "unsupported"
                ),
                "probe": probe_status.get(intent.id),
            }
            decided = {v for v in row.values() if v in {"passed", "failed"}}
            row["agree"] = len(decided) <= 1
            rows[intent.id] = row
        (self.root / f"{stage}.json").write_text(json.dumps(rows, indent=2) + "\n")
        self.summary[stage] = {
            "intents": len(rows),
            "disagreements": sorted(i for i, row in rows.items() if not row["agree"]),
            **({"fib_scope": self.limit} if self.limit else {}),
            **({"fib_error": error} if error else {}),
        }
