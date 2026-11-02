"""Traffic-control units and per-class evidence used by overlay QoS faults."""

import json

import pytest

from nika.problems.traffic_queueing_resource.overlay_qos import linux_queue_state

pytestmark = pytest.mark.unit


def test_tc_queue_rates_are_bytes_per_second_and_counters_are_per_class():
    output = json.dumps(
        [
            {
                "handle": "1:20",
                "rate": 500000,
                "ceil": 500000,
                "stats": {"bytes": 9000, "packets": 7, "drops": 0},
            },
            {
                "handle": "1:10",
                "rate": 50000,
                "ceil": 50000,
                "stats": {"bytes": 2464, "packets": 2, "drops": 18},
            },
        ]
    )
    assert linux_queue_state(output, "1:10") == {
        "rate_kbps": 400,
        "ceil_kbps": 400,
        "bytes": 2464,
        "packets": 2,
        "drops": 18,
    }
