"""A periodic carrier can sustain an IP outage while ARP is unresolved."""

from types import SimpleNamespace

import pytest

from nika.problems.rca.inventory import load_offline_net_env
from nika.validation.symptom.flap_probes import evaluate_link_flap_symptom

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("carrier", "replies", "expected"),
    [
        ("000111" * 4, "." * 24, True),
        ("1" * 24, "." * 24, False),
        ("0" * 24, "." * 24, False),
        ("000111" * 2, "." * 12, False),
        ("000111" * 4, "!" * 24, False),
        ("???111" + "000111" * 3, "." * 24, False),
        ("1" * 24, "...!!!" * 4, True),
    ],
)
def test_outage_requires_observed_periodic_carrier(carrier, replies, expected):
    class Runtime:
        def exec(self, host, command, **kwargs):
            if "while " in command:
                if "/carrier" in command:
                    return "\n".join(a + b for a, b in zip(carrier, replies))
                return replies
            return ""

    env = load_offline_net_env("dc_clos", topo_size="s")
    problem = SimpleNamespace(
        net_env=env,
        runtime=Runtime(),
        root_cause_name="link_flap",
        _audit_flap_before={
            "src": "client_1_1",
            "dst": "10.0.2.11",
            "loss_percent": 0,
            "ttls": [61],
        },
    )
    params = SimpleNamespace(
        host_name="client_1_1", intf_name="eth0", down_time=1, up_time=1
    )
    verified, payload = evaluate_link_flap_symptom(problem, params)
    assert verified is expected, payload
