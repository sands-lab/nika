"""CoreDNS freshness uses the uptime-agent's elapsed clock."""

import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika.validation.symptom.coredns_loop_probes import forward_loop_baseline

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("wall_epoch", [1794880000, 4102444800])
@pytest.mark.parametrize("age", [0, 120])
def test_freshness_survives_wall_clock_changes_and_rejects_stale_probes(
    tmp_path, wall_epoch, age
):
    stamp = tmp_path / "last-ok"
    stamp.write_text(str(int(float(Path("/proc/uptime").read_text().split()[0])) - age))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, output in {"date": str(wall_epoch), "nslookup": "DNS resolved"}.items():
        executable = binaries / name
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        executable.chmod(0o755)
    ready = {"conditions": [{"type": "Ready", "status": "True"}], "phase": "Running"}
    pods = [
        {
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "status": ready,
        }
        for name, namespace, labels in [
            ("dns", "kube-system", {"k8s-app": "kube-dns"}),
            ("agent", "monitoring", {"app": "uptime-agent"}),
            ("app", "application", {}),
        ]
    ]

    def kubectl(_control, arguments, **_kwargs):
        script = shlex.split(arguments)[-1].replace(
            "/tmp/last-ok", shlex.quote(str(stamp))
        )
        completed = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]},
        )
        return SimpleNamespace(
            stdout=completed.stdout, stderr=completed.stderr, ok=True
        )

    api = SimpleNamespace(
        kubectl_json=lambda _control, query: {
            "items": [{"status": ready}] if query == "get nodes" else pods
        },
        kubectl=kubectl,
    )
    problem = SimpleNamespace(
        runtime=SimpleNamespace(lab_api=api), control_node=lambda _: "controller"
    )
    healthy, evidence = forward_loop_baseline(problem, None)
    assert healthy is (age == 0)
    assert age <= evidence["last_success_age_sec"] <= age + 5
