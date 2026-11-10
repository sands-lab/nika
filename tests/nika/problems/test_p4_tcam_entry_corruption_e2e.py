"""A TCAM mask mutation affects its target, preserves peers, and recovers."""

import pytest

from nika.net_env.p4_dc_fabric.fabric_manager.apply import load_intent
from nika.problems.registry import get_problem_class
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


@pytest.mark.e2e
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4TcamEntryCorruptionE2ETest(IntegrationTestCase):
    def test_mask_mutation_and_recovery(self):
        session_id = None
        try:
            session_id = self._start_env("p4_dc_gateway", ["-s", "s"])
            self._assert_session_ready(session_id, "p4_dc_gateway")
            problem = self._problem(
                get_problem_class("p4_tcam_entry_corruption"), session_id=session_id
            )
            params = problem.parse_params(
                dict(
                    host_name="gateway_1",
                    target_ip="10.0.1.11",
                    control_source="client_2",
                )
            )
            runtime = problem.runtime
            intent = load_intent(runtime)

            def loss(ip):
                output = runtime.exec(
                    "client_1", f"ping -c 3 -i 0.2 -W 1 {ip}", timeout=10
                )
                from nika.service.pingmesh.parser import parse_ping_output

                return parse_ping_output(output)["loss_percent"]

            assert loss(params.target_ip) == 0
            assert loss("10.0.1.12") == 0
            assert not problem.verify_fault(params)["verified"]
            try:
                problem.inject_fault(params)
                assert problem.verify_fault(params)["verified"]
                assert load_intent(runtime) == intent
                assert loss(params.target_ip) == 100
                assert loss("10.0.1.12") == 0
                # The reference diagnosis must read the changed device mask.
                playbook = problem.oracle_playbook(params)
                evidence = [step for step in playbook.steps if step.evidence]
                assert evidence
                from nika.service.kathara import KatharaBMv2API

                api = KatharaBMv2API(lab_name=runtime.lab_name)
                for step in evidence:
                    output = api.p4rt_exec(**step.args)
                    assert step.expect is not None and step.expect(output), output
            finally:
                assert problem.recover_fault(params)["verified"]
            assert problem.recover_fault(params)["verified"]
            assert loss(params.target_ip) == 0
            assert loss("10.0.1.12") == 0
        finally:
            if session_id:
                self._close_session(session_id)
