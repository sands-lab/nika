"""Parsing behind the incast fault's verify_fault and symptom probe."""

import pytest

from nika.problems.traffic_queueing_resource.link import (
    tbf_shape,
    tc_rate_bps,
    tc_size_bytes,
)

pytestmark = pytest.mark.unit


def test_tbf_shape_recovers_queue_limit_from_reported_latency() -> None:
    # ``tc -j qdisc show`` for ``tbf rate 20mbit burst 2kb limit 16kb``.
    output = (
        '[{"kind":"tbf","handle":"b7f8:","root":true,"refcnt":2,'
        '"options":{"rate":2500000,"burst":2048,"lat":5734}}]'
    )
    shape = tbf_shape(output)
    assert shape is not None
    assert shape["rate_bps"] == tc_rate_bps("20mbit")
    assert abs(shape["limit_bytes"] - tc_size_bytes("16kb")) <= 8


def test_tbf_shape_ignores_other_root_qdiscs() -> None:
    output = '[{"kind":"fq_codel","handle":"0:","root":true,"options":{}}]'
    assert tbf_shape(output) is None
    assert tbf_shape("Cannot find device") is None
