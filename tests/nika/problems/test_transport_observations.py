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
        ("LISTEN 0 5 0.0.0.0:80 0.0.0.0:*", True),
        ("UNCONN 0 0 0.0.0.0:80 0.0.0.0:*", False),
        ("", False),
        (None, None),
    ],
)
def test_sender_verification_requires_successful_listen_observation(
    monkeypatch, output, expected
):
    problem = object.__new__(transport.EndpointCPUContention)
    problem.runtime = SimpleNamespace()
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

    def listeners(*args):
        if output is None:
            raise subprocess.CalledProcessError(127, "ss")
        return output

    monkeypatch.setattr(transport, "container_tcp_listeners", listeners)
    params = transport.EndpointCPUContentionParams(host_name="web")
    if expected is None:
        with pytest.raises(subprocess.CalledProcessError):
            problem.verify_fault(params)
    else:
        result = problem.verify_fault(params)
        assert result["verified"] is expected
        assert result["details"]["http_running"] is expected


@pytest.mark.parametrize("window_share,expected", [(0.0, False), (0.8, True)])
def test_receiver_cpu_contention_checks_path_from_the_healthy_peer(
    monkeypatch, window_share, expected
):
    from types import SimpleNamespace

    from nika.validation.symptom import perf_probes

    observers = []
    problem = SimpleNamespace(
        runtime=SimpleNamespace(get_host_ip=lambda *args, **kwargs: "192.0.2.10"),
        root_cause_name="endpoint_cpu_contention",
        _baseline_throughput_bps=100.0,
        _baseline_time_s=1.0,
        _baseline_rtt_ms=1.0,
        _audit_receiver_baseline={
            "sender": "server",
            "receiver_ip": "192.0.2.10",
            "median_bps": 100.0,
        },
    )
    params = transport.EndpointCPUContentionParams(
        host_name="client", client_host="client", server_host="server"
    )

    def ping(runtime, source, destination, **kwargs):
        observers.append((source, destination))
        return SimpleNamespace(loss_percent=0.0, rtt_avg_ms=1.0)

    monkeypatch.setattr(perf_probes, "ping_stats", ping)
    monkeypatch.setattr(
        perf_probes,
        "_stream",
        lambda runtime, sender, ip: {
            "ok": True,
            "bps": 40.0,
            "rwnd_limited_share": window_share,
        },
    )
    assert perf_probes.sender_contention(problem, params)[0] is expected
    assert observers == [("server", "192.0.2.10")]


@pytest.mark.parametrize("host", ["web", "client"])
def test_cpu_endpoint_defaults_preserve_explicit_parameters(host):
    problem = transport.EndpointCPUContention("dc_clos")
    problem.net_env = SimpleNamespace(
        model=SimpleNamespace(
            web_endpoints=lambda: [
                SimpleNamespace(name="web", ip="192.0.2.1", leaf_id=1)
            ],
            client_endpoints=lambda: [SimpleNamespace(name="client", leaf_id=2)],
        )
    )
    explicit = dict(
        client_host="client_0",
        dst_ip="10.0.1.2",
        small_url="http://web0.pod0/small.bin",
        large_url="http://custom/large.bin",
    )
    params = problem.parse_params(dict(host_name=host, **explicit))
    assert {key: getattr(params, key) for key in explicit} == explicit
    defaults = problem.parse_params(dict(host_name=host))
    assert defaults.client_host == "client"
    assert defaults.dst_ip == "192.0.2.1"
    assert defaults.server_host == ("web" if host == "client" else None)
    transport.EndpointCPUContention.validate_benchmark_inject(
        SimpleNamespace(net_env=problem.net_env), dict(host_name=host)
    )
    with pytest.raises(ValueError, match="Unknown CPU contention endpoint"):
        problem.parse_params(dict(host_name="typo"))
    with pytest.raises(ValueError, match="Unknown CPU contention endpoint"):
        transport.EndpointCPUContention.validate_benchmark_inject(
            SimpleNamespace(net_env=problem.net_env), dict(host_name="typo")
        )
