"""NAT validation parses service state and requires declared capabilities."""

from types import SimpleNamespace

import pytest

from nika.runtime.base import RuntimeCapabilityError
from nika.traffic.workload import BusinessWorkload, WorkloadProcess
from nika.validation.symptom.nat_probes import _connections, _service_port

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "row,peer",
    [
        ("0 128 198.51.100.10:80 198.18.1.10:45000", "198.18.1.10:45000"),
        (
            "ESTAB 0 128 [::ffff:198.51.100.10]:80 [::ffff:198.18.1.10]:45000",
            "[::ffff:198.18.1.10]:45000",
        ),
    ],
)
def test_nat_service_socket_state_keeps_peer_and_undelivered_bytes(row, peer):
    runtime = SimpleNamespace(exec=lambda *_args, **_kwargs: row)
    assert _connections(
        runtime, {"server": "service", "port": "80"}, "198.18.1.10"
    ) == {peer: 128}


def test_nat_service_socket_state_rejects_errors():
    runtime = SimpleNamespace(exec=lambda *_args, **_kwargs: "ss: command not found")
    with pytest.raises(ValueError, match="TCP socket state"):
        _connections(runtime, {"server": "service", "port": "80"}, "198.18.1.10")


def test_nat_checks_require_the_declared_service():
    def problem(processes):
        workload = BusinessWorkload(tuple(processes))
        return SimpleNamespace(
            net_env=SimpleNamespace(business_workload=lambda: workload)
        )

    service = WorkloadProcess("public", "web_service", "webapp", ("--port", "8080"))
    assert _service_port(problem([service]), "public") == 8080
    with pytest.raises(RuntimeCapabilityError, match="declared HTTP service"):
        _service_port(problem([]), "public")
