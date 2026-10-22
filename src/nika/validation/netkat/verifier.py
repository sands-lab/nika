"""Evaluate original packet predicates with the upstream NetKAT decision procedure."""

import json
import re
from pathlib import Path
from time import monotonic
from uuid import uuid4

import docker

from nika.net_env.contract import ValidationReport, ValidationResult
from nika.validation.netkat.model import ForwardingNetwork, prefix_test

KATCH_IMAGE = "julesjacobs/katch@sha256:7184375abfbcf38e6c1e176637507272006d4e4bfa8ce30dab60069dcd6da410"
KATCH_JAR = "/katch/target/scala-3.3.1/KATch-assembly-0.1.0-SNAPSHOT.jar"


def _queries(intent, network):
    if intent.property not in {"reachability", "isolation", "waypoint"}:
        raise ValueError(
            "Only packet reachability, isolation and waypoint predicates are supported"
        )
    if not intent.traffic or intent.traffic.protocol != "icmp":
        raise ValueError("This forwarding slice requires an explicit ICMP selector")
    if intent.property == "isolation" and intent.traffic.round_trip:
        raise ValueError(
            "Isolation uses a one-way ICMP selector in this forwarding slice"
        )
    source, destination = intent.source, intent.destination
    if source is None or destination is None or source.name not in network.endpoints:
        raise ValueError("A concrete source endpoint and destination are required")
    if not destination.address:
        raise ValueError("Destination address is required")
    if "/" in destination.address:
        raise ValueError(
            "Packet predicates require concrete destination IPv4 addresses"
        )
    if source.node not in {None, source.name} or destination.node not in {
        None,
        destination.name,
    }:
        raise ValueError(
            "Native node bindings must match the original endpoint identities"
        )
    if network.endpoints[source.name].address != source.address:
        raise ValueError("Source address differs from the original endpoint attachment")
    ids = network.location_ids()
    if (
        destination.name in network.endpoints
        and network.endpoints[destination.name].address != destination.address
    ):
        raise ValueError("Destination address differs from its endpoint attachment")
    target = (
        f"@sw={ids[destination.name]}" if destination.name in network.endpoints else "⊤"
    )
    select = f"@sw={ids[source.name]} ⋅ @done=0 ⋅ ({prefix_test(destination.address)})"
    path = f"({select}) ⋅ net⋆ ⋅ @done=1 ⋅ ({target})"
    checks = [(path, "≡" if intent.property == "isolation" else "≢")]
    if intent.path:
        for node in intent.path.must_avoid:
            if node not in network.rules:
                raise ValueError(f"Unknown waypoint: {node}")
            checks.append(
                (
                    f"({select}) ⋅ net⋆ ⋅ @sw={ids[node]} ⋅ net⋆ ⋅ @done=1 ⋅ ({target})",
                    "≡",
                )
            )
        for node in intent.path.must_traverse:
            if node not in network.rules:
                raise ValueError(f"Unknown waypoint: {node}")
            checks.append(
                (
                    f"({select}) ⋅ ((@sw≠{ids[node]}) ⋅ net)⋆ ⋅ @done=1 ⋅ ({target})",
                    "≡",
                )
            )
    if intent.traffic.round_trip:
        if destination.name not in network.endpoints:
            raise ValueError("Round-trip checks require two concrete endpoints")
        checks.append(
            (
                f"@sw={ids[destination.name]} ⋅ @done=0 ⋅ ({prefix_test(source.address)}) ⋅ net⋆ ⋅ @done=1 ⋅ @sw={ids[source.name]}",
                "≢",
            )
        )
    return checks


class NetKatVerifier:
    name = "netkat"
    supported_properties = frozenset({"reachability", "isolation", "waypoint"})

    def __init__(self, *, image=KATCH_IMAGE, timeout=300):
        self.image = image
        self.timeout = timeout

    def verify(self, contract, snapshot):
        started = monotonic()
        network = ForwardingNetwork.model_validate_json(
            (snapshot.path / "network.json").read_text()
        )
        relation = network.relation()
        pending, outcomes, unsupported = [], {}, {}
        for intent in contract.intents:
            try:
                checks = _queries(intent, network)
            except ValueError as exc:
                unsupported[intent.id] = str(exc)
                continue
            for index, (query, op) in enumerate(checks):
                pending.append(((intent.id, index), f"check {query} {op} ⊥"))
        client = None
        try:
            if pending:
                query_dir = snapshot.path / "queries"
                query_dir.mkdir(exist_ok=True)
                for old in query_dir.glob("query-*.nkpl"):
                    old.unlink()
                (snapshot.path / "network.nkpl").write_text(f"net = {relation}\n")
                for index, (_, query) in enumerate(pending):
                    (query_dir / f"query-{index:06d}.nkpl").write_text(query + "\n")
                remaining = self.timeout - (monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("KATch analysis exceeded its time budget")
                client = docker.from_env()
                container = client.containers.create(
                    self.image,
                    entrypoint=["/bin/sh", "-c"],
                    command=[
                        f"javac -J-Xmx256m -cp {KATCH_JAR} -d /tmp /adapter/KatchBatch.java"
                        f" && exec java -Xmx1536m -Xss10m -cp /tmp:{KATCH_JAR}"
                        " KatchBatch /work/network.nkpl /work/queries"
                    ],
                    name=f"nika-katch-{uuid4().hex[:12]}",
                    network_disabled=True,
                    mem_limit="2g",
                    nano_cpus=2_000_000_000,
                    volumes={
                        str(snapshot.path.resolve()): {"bind": "/work", "mode": "ro"},
                        str(Path(__file__).resolve().parent): {
                            "bind": "/adapter",
                            "mode": "ro",
                        },
                    },
                )
                try:
                    container.start()
                    try:
                        status = container.wait(timeout=remaining)["StatusCode"]
                    finally:
                        log = container.logs().decode(errors="replace")
                        (snapshot.path / "batch.log").write_text(log)
                finally:
                    container.remove(force=True)
                clean = re.sub(r"\x1b\[[0-9;]*m", "", log)
                matched = {}
                for result, number, line in re.findall(
                    r"Check (passed|failed) in /work/queries/query-(\d+)\.nkpl:(\d+)",
                    clean,
                ):
                    index = int(number)
                    if not 0 <= index < len(pending) or index in matched or line != "1":
                        raise RuntimeError("Unexpected KATch result location")
                    matched[index] = result
                if status != 0 or len(matched) != len(pending):
                    raise RuntimeError(
                        "KATch batch failed or is incomplete; see batch.log"
                    )
                for index, result in matched.items():
                    outcomes[pending[index][0]] = result
        except Exception as exc:
            for key, _ in pending:
                outcomes[key] = "error"
            error = str(exc)
        else:
            error = None
        finally:
            if client is not None:
                client.close()
        results = []
        for intent in contract.intents:
            values = [v for (name, _), v in outcomes.items() if name == intent.id]
            status = (
                "unsupported"
                if intent.id in unsupported
                else "error"
                if "error" in values or not values
                else "failed"
                if "failed" in values
                else "passed"
            )
            results.append(
                ValidationResult(
                    intent=intent.id,
                    verifier=self.name,
                    status=status,
                    supported=status != "unsupported",
                    reason=unsupported.get(intent.id)
                    or (error if status == "error" else None),
                    duration_ms=(monotonic() - started) * 1000,
                    evidence={"checks": values},
                )
            )
        report = ValidationReport.from_results(
            contract,
            self.name,
            results,
            metadata={
                "tool_image": self.image,
                "assumptions": network.assumptions,
                **network.metadata,
            },
        )
        if report.status == "passed" and any(
            i.level == "required" and i.id in unsupported for i in contract.intents
        ):
            report = report.model_copy(update={"status": "unsupported"})
        return report


def write_snapshot(network, root):
    from nika.validation.base import ValidationSnapshot

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for pattern in ("batch.log", "network.nkpl"):
        for old in root.glob(pattern):
            old.unlink()
    for old in (root / "queries").glob("query-*.nkpl"):
        old.unlink()
    (root / "network.json").write_text(
        json.dumps(network.model_dump(mode="json"), indent=2) + "\n"
    )
    return ValidationSnapshot(
        snapshot_id=root.name, path=root, metadata=network.metadata
    )
