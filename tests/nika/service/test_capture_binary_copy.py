"""Capture copies preserve binary bytes from the live mount namespace."""

from types import SimpleNamespace
import pytest
from nika.service.packet_capture.inspect import _read_pcap

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "payload", [b"\xd4\xc3\xb2\xa1\x00\xff", b"\x0a\x0d\x0d\x0a\x00\x80"]
)
def test_capture_copy_preserves_binary_bytes_from_the_live_mount(payload):
    seen = []

    class Container:
        def exec_run(self, args, **kwargs):
            seen.append((args, kwargs))
            return SimpleNamespace(exit_code=0, output=(payload, b"warning"))

        def get_archive(self, path):
            raise AssertionError("archive cannot see the node's private mount")

    runtime = SimpleNamespace(get_container=lambda node: Container())
    assert _read_pcap(runtime, "spine", "/tmp/capture.pcapng") == payload
    assert seen == [(["cat", "--", "/tmp/capture.pcapng"], {"demux": True})]


@pytest.mark.parametrize(
    "code,stdout,stderr", [(1, None, b"not found"), (0, b"", None)]
)
def test_capture_copy_does_not_treat_failed_or_empty_reads_as_packets(
    code, stdout, stderr
):
    result = SimpleNamespace(exit_code=code, output=(stdout, stderr))
    runtime = SimpleNamespace(
        get_container=lambda node: SimpleNamespace(exec_run=lambda *a, **k: result)
    )
    with pytest.raises((RuntimeError, ValueError)):
        _read_pcap(runtime, "spine", "/tmp/missing.pcapng")
