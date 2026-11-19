"""Monitoring readiness follows completed checks and atomic timestamp updates."""

import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from nika.config import pkg_path

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "manifest",
    ["k8s_lab/shared/uptime-agent.yaml", "llmd_lab/controller/k8s/uptime-agent.yaml"],
)
def test_monitor_requires_a_successful_round_and_keeps_last_reading(tmp_path, manifest):
    daemonset = list(yaml.safe_load_all(pkg_path("net_env", manifest).read_text()))[1]
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    timestamp = tmp_path / "last-ok"
    script = container["args"][0].replace("/tmp/last-ok", str(timestamp))
    script = script.replace("sleep 10", "break")
    probe = container["readinessProbe"]["exec"]["command"][-1].replace(
        "/tmp/last-ok", str(timestamp)
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    wget = binaries / "wget"
    wget.write_text("#!/bin/sh\nexit 1\n")
    wget.chmod(0o755)
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"}

    subprocess.run(["sh", "-c", script], env=env, check=True, timeout=5)
    assert not timestamp.exists()
    assert subprocess.run(["sh", "-c", probe], timeout=5).returncode != 0

    wget.write_text("#!/bin/sh\nexit 0\n")
    timestamp.write_text(f"{int(float(Path('/proc/uptime').read_text().split()[0]))}\n")
    started = tmp_path / "updating"
    release = tmp_path / "release"
    cut = binaries / "cut"
    cut.write_text(
        f"#!/bin/sh\ntouch {started}\n"
        f'while [ ! -f {release} ]; do sleep 0.01; done\nexec /usr/bin/cut "$@"\n'
    )
    cut.chmod(0o755)
    process = subprocess.Popen(["sh", "-c", script], env=env)
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        assert subprocess.run(["sh", "-c", probe], timeout=5).returncode == 0
    finally:
        release.touch()
        process.wait(timeout=5)
    assert process.returncode == 0
    assert subprocess.run(["sh", "-c", probe], timeout=5).returncode == 0


@pytest.mark.parametrize(
    "manifest",
    ["k8s_lab/shared/uptime-agent.yaml", "llmd_lab/controller/k8s/uptime-agent.yaml"],
)
@pytest.mark.parametrize("wall_offset", [-86400, 86400])
def test_monitor_health_uses_elapsed_guest_time_across_wall_clock_changes(
    tmp_path, manifest, wall_offset
):
    daemonset = list(yaml.safe_load_all(pkg_path("net_env", manifest).read_text()))[1]
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    timestamp = tmp_path / "last-ok"
    probe = container["readinessProbe"]["exec"]["command"][-1].replace(
        "/tmp/last-ok", str(timestamp)
    )
    date = tmp_path / "date"
    date.write_text(f"#!/bin/sh\necho {int(time.time()) + wall_offset}\n")
    date.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    now = int(float(Path("/proc/uptime").read_text().split()[0]))
    timestamp.write_text(f"{now}\n")
    assert subprocess.run(["sh", "-c", probe], env=env, timeout=5).returncode == 0
    timestamp.write_text(f"{now - 61}\n")
    assert subprocess.run(["sh", "-c", probe], env=env, timeout=5).returncode != 0
