"""Contention injection requires readable configuration and a TCP listener."""

import subprocess
from types import SimpleNamespace

import pytest

from nika.problems.endpoint_application import transport

pytestmark = pytest.mark.unit


def test_archive_configuration_must_exist_before_injection(tmp_path, monkeypatch):
    path = tmp_path / "archive-job.json"
    monkeypatch.setattr(transport, "_ARCHIVE_CONFIG", str(path))

    class Runtime:
        def exec(self, node, command):
            return subprocess.run(
                command, shell=True, capture_output=True, text=True
            ).stdout

        def write_file(self, node, target, content):
            assert target == str(path)
            path.write_text(content)

    runtime = Runtime()
    with pytest.raises(RuntimeError, match="configuration .* is absent"):
        transport._stage_archive_job(runtime, "web", 16)
    assert not path.exists()
    original = '{"workers": 1}\n\n'
    path.write_text(original)
    assert transport._stage_archive_job(runtime, "web", 16) == {"config": original}
    assert path.read_text() == '{"workers": 16}\n'


@pytest.mark.parametrize(
    "output,expected",
    [
        ("LISTEN 0 5 0.0.0.0:80 0.0.0.0:*\n__rc=0", True),
        ("UNCONN 0 0 0.0.0.0:80 0.0.0.0:*\n__rc=0", False),
        ("\n__rc=0", False),
        ("sh: ss: not found\n__rc=127", None),
    ],
)
def test_sender_verification_requires_successful_listen_observation(
    monkeypatch, output, expected
):
    problem = object.__new__(transport.SenderResourceContention)
    problem.runtime = SimpleNamespace(exec=lambda *args, **kwargs: output)
    problem._injected_nano_cpus = 50_000_000
    monkeypatch.setattr(
        transport,
        "container_commands",
        lambda *args: [
            "python3 /usr/local/bin/archive-job.py run",
            "gzip -c input.dat",
        ],
    )
    monkeypatch.setattr(transport, "read_nano_cpus", lambda *args: 50_000_000)
    params = transport.SenderResourceContentionParams(host_name="web")
    if expected is None:
        with pytest.raises(RuntimeError, match="rc=127"):
            problem.verify_fault(params)
    else:
        result = problem.verify_fault(params)
        assert result["verified"] is expected
        assert result["details"]["http_running"] is expected
