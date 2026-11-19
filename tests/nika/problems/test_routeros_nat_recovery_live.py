"""Native NAT recovery restores policy and the withdrawn public address."""

import pytest

from nika.net_env.utils.routeros.common import IMAGE
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not docker_available() or not docker_image_available(IMAGE),
    reason="Requires Docker and the RouterOS image",
)
def test_routeros_nat_failures_restore_exact_rules_and_wan_addresses():
    from nika.problems.service_networking.gateway import native_fault_ops

    sid = start_net_env(
        "enterprise_branch",
        "s",
        device_profile="routeros",
        telemetry=[],
        session_tag=resolve_session_tag(context="test"),
    )
    runtime = None
    failure = None
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)
        cases = [
            (
                "nat_mapping_removed_without_drain",
                {
                    "host_name": "br1_edge",
                    "nat_ip_a": "198.18.1.10",
                    "nat_ip_b": "198.18.1.11",
                    "wan_interface": "eth2",
                    "source_prefix": "10.1.40.0/24",
                },
            ),
            (
                "snat_port_pool_exhaustion",
                {"host_name": "br1_edge", "public_ip": "198.18.1.10"},
            ),
        ]
        for name, params in cases:
            failure = prepare_failure(session, [name], params)
            ops = native_fault_ops(failure.problem, "br1_edge", "source_nat_rules")
            before_rules = ops.source_nat_rules()
            before_addresses = ops.interface_addresses("eth2")
            failure.inject()
            assert failure.verify()["verified"]
            assert ops.source_nat_rules() != before_rules
            if name == "nat_mapping_removed_without_drain":
                assert "198.18.1.10/32" not in ops.interface_addresses("eth2")
            assert failure.recover()["verified"]
            assert ops.source_nat_rules() == before_rules
            assert ops.interface_addresses("eth2") == before_addresses
            assert "2 received" in runtime.exec(
                "br1_guest_pc", "ping -c 2 -W 2 198.51.100.10", timeout=10
            )
            failure = None
    finally:
        try:
            if failure is not None:
                failure.recover()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
