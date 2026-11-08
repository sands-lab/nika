"""Communication requirements authored over the network design."""

from nika.net_env.contract import (
    NetworkEntity,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
)


def communication_contract(model, *, scenario, topo_size):
    """Require client-service communication and isolation from an unowned prefix."""
    endpoints = {e.name: e for e in model.endpoints}
    clients = {
        e.name for e in model.endpoints if e.role in {"client", "external_client"}
    }
    services = {e.name for e in model.endpoints if e.role in {"web", "http_service"}}
    if clients | services != endpoints.keys() or not clients or not services:
        raise ValueError(
            "Declare client and service roles before constructing the communication specification"
        )
    intents = []
    for source, src in endpoints.items():
        entity = NetworkEntity(
            kind="endpoint", name=source, address=src.ip, node=source
        )
        for destination, dst in endpoints.items():
            if not (
                (source in clients and destination in services)
                or (source in services and destination in clients)
            ):
                continue
            intents.append(
                ValidationIntent(
                    id=f"reach.{source}.{destination}",
                    description="Declared client-service communication in the fabric",
                    property="reachability",
                    expected="reachable",
                    source=entity,
                    destination=NetworkEntity(
                        kind="endpoint",
                        name=destination,
                        address=dst.ip,
                        node=destination,
                    ),
                    traffic=TrafficSelector(protocol="icmp", round_trip=True),
                )
            )
        intents.append(
            ValidationIntent(
                id=f"isolation.{source}.unowned",
                description="The fabric must not deliver traffic to the unowned test address",
                property="isolation",
                expected="unreachable",
                source=entity,
                destination=NetworkEntity(
                    kind="endpoint", name="unowned", address="198.18.0.1"
                ),
                traffic=TrafficSelector(protocol="icmp"),
            )
        )
    healthy = ValidationContract(
        contract_id="fabric-healthy",
        scenario=scenario,
        design_source={
            "method": "Authored client-service ICMP communication and unowned-prefix isolation",
            "client_endpoints": sorted(clients),
            "service_endpoints": sorted(services),
            "topology_model": type(model).__name__,
            "topo_size": topo_size,
        },
        intents=tuple(sorted(intents, key=lambda i: i.id)),
    )
    return healthy
