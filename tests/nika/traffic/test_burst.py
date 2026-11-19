"""Burst packet pacing and process ownership without persistent helper files."""

import os
import select
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from nika.runtime.shared.processes import process_identity, process_source
from nika.traffic.burst import BurstTrafficGenerator, PreparedBurst, _RAW_UDP_SENDER


@pytest.mark.unit
@pytest.mark.parametrize("error", [RuntimeError("unreachable"), KeyboardInterrupt()])
def test_prepared_burst_attempts_all_owned_processes_when_stop_fails(error):
    runtime = Mock()
    runtime.exec.side_effect = [error, "stopped\n"]
    first = ("first", [1, 2, 3])
    second = ("second", [4, 5, 6])
    burst = PreparedBurst(runtime, [], "127.0.0.1", [first, second])

    with pytest.raises(type(error)):
        burst.stop()

    assert [call.args[0] for call in runtime.exec.call_args_list] == ["first", "second"]
    assert burst.processes == [first]


@pytest.mark.unit
def test_prepared_burst_retains_process_if_stop_command_fails():
    runtime = Mock()
    runtime.exec.return_value = "process cleanup failed"
    process = ("host", [1, 2, 3])
    burst = PreparedBurst(runtime, [], "127.0.0.1", [process])

    with pytest.raises(RuntimeError, match="Could not stop owned burst process"):
        burst.stop()

    assert burst.processes == [process]


@pytest.mark.unit
def test_standalone_burst_cleans_receivers_when_ecn_read_fails():
    runtime = Mock()
    runtime.get_data_plane_host_ip.side_effect = ["192.0.2.2", "192.0.2.1"]
    runtime.exec.side_effect = ['[123, 456, "boot"]', "read failed", "stopped"]

    with pytest.raises(RuntimeError, match="Could not read TCP ECN"):
        BurstTrafficGenerator(runtime).run(
            sources=["source"],
            destination="destination",
            protocol="tcp",
            rate="1M",
            packet_size=128,
            duration=1,
            synchronized_start=0,
            seed=1,
        )

    commands = [call.args[1] for call in runtime.exec.call_args_list]
    assert not any("sysctl -w" in command for command in commands)
    assert "stop_owned([123, 456, 'boot'])" in shlex.split(commands[-1])[2]


@pytest.mark.unit
@pytest.mark.parametrize("rate", ["8K/0", "0/4", "invalid"])
def test_invalid_raw_rate_is_rejected_before_starting_receivers(rate):
    runtime = Mock()
    with pytest.raises(ValueError):
        BurstTrafficGenerator(runtime).run(
            sources=["a"],
            destination="b",
            protocol="udp",
            rate=rate,
            packet_size=128,
            duration=1,
            synchronized_start=0,
            seed=1,
            raw_udp=True,
        )
    assert not runtime.mock_calls


@pytest.mark.integration
def test_raw_udp_senders_share_start_and_send_packet_groups_with_quiet_intervals():
    sockets, senders = [], []
    start = time.time() + 0.6
    try:
        for _ in range(2):
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(receiver)
            receiver.bind(("127.0.0.1", 0))
            senders.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _RAW_UDP_SENDER,
                        "127.0.0.1",
                        str(receiver.getsockname()[1]),
                        "8192",
                        "128",
                        "1.2",
                        "0",
                        "4",
                        str(start),
                    ]
                )
            )
        arrivals = {s: [] for s in sockets}
        deadline = start + 1.6
        while time.time() < deadline:
            ready, _, _ = select.select(sockets, [], [], max(0, deadline - time.time()))
            for receiver in ready:
                data, _ = receiver.recvfrom(65535)
                assert data == b"x" * 128
                arrivals[receiver].append(time.time())
        first = []
        for times in arrivals.values():
            assert len(times) == 12, times
            first.append(times[0])
            assert start - 0.02 <= times[0] <= start + 0.25
            groups = [1]
            for previous, current in zip(times, times[1:]):
                if current - previous > 0.2:
                    groups.append(1)
                else:
                    groups[-1] += 1
            assert groups == [4, 4, 4], times
        assert max(first) - min(first) < 0.25
        for sender in senders:
            assert sender.wait(timeout=2) == 0
    finally:
        for sender in senders:
            if sender.poll() is None:
                sender.terminate()
            sender.wait(timeout=2)
        for receiver in sockets:
            receiver.close()


@pytest.mark.integration
def test_prepared_burst_stops_only_owned_birth_identity_and_leaves_no_logs(tmp_path):
    class Runtime:
        def exec(self, node, command, timeout):
            return subprocess.run(
                ["sh", "-c", command],
                cwd=tmp_path,
                timeout=timeout,
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": "/usr/bin:/bin"},
            ).stdout

    def alive(identity):
        try:
            return (
                process_identity(identity[0]) == identity
                and Path(f"/proc/{identity[0]}/stat")
                .read_text()
                .rsplit(") ", 1)[1]
                .split()[0]
                != "Z"
            )
        except (FileNotFoundError, ProcessLookupError):
            return False

    burst = PreparedBurst(Runtime(), [], "127.0.0.1")
    original = None
    try:
        burst.launch("local", [sys.executable, "-c", "import time; time.sleep(60)"])
        host, original = burst.processes[0]
        assert alive(original)
        assert Path(f"/proc/{original[0]}/fd/1").resolve() == Path("/dev/null")
        wrong_birth = [original[0], original[1] + 1, original[2]]
        burst.processes[:] = [(host, wrong_birth)]
        burst.stop()
        assert alive(original)
        burst.processes[:] = [(host, original)]
        burst.stop()
        assert not alive(original)
        burst.stop()
        assert not burst.processes
        assert not list(tmp_path.iterdir())
    finally:
        if original:
            source = process_source() + f"\nstop_owned({original!r})\n"
            burst.runtime.exec(
                "local", shlex.join(["python3", "-c", source]), timeout=15
            )
