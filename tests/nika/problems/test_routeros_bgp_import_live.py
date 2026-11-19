"""RouterOS applies and recovers IPv4 import-range changes on live sessions."""

import time

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
def test_routeros_prefix_range_refreshes_ipv4_and_recovers():
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
        failure = prepare_failure(
            session,
            ["bgp_prefix_list_length_mismatch"],
            {"host_name": "br1_edge"},
        )
        problem = failure.problem
        peers, routes = problem._state("br1_edge")
        assert peers and routes
        failure.inject()
        assert failure.verify()["verified"]
        deadline = time.monotonic() + 30
        while True:
            live_peers, live_routes = problem._state("br1_edge")
            if routes - live_routes:
                break
            assert time.monotonic() < deadline, (
                "IPv4 import refresh did not remove routes"
            )
            time.sleep(1)
        assert live_peers == peers
        assert failure.recover()["verified"]
        assert problem._state("br1_edge") == (peers, routes)
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
