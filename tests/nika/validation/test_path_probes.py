"""Path probes count replies from both iputils and BusyBox hosts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.validation.symptom.path_probes import echo

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("sequence", ["icmp_seq", "seq"])
def test_echo_counts_unique_replies_from_linux_and_k3s(sequence):
    output = "\n".join(
        f"64 bytes from 192.0.2.1: {sequence}={seq} ttl=63 time=1.2 ms"
        for seq in (0, 1, 1)
    )
    runtime = SimpleNamespace(exec=Mock(return_value=output))
    sample = echo(runtime, "sender", "192.0.2.1", count=3)
    assert sample["received"] == 2
    assert sample["loss_percent"] == 33.3
    assert sample["ttls"] == [63]
