"""Benchmark contract: what an agent can read in a lab does not name NIKA.

Agents read the task brief and inspect devices with shell tools. Neither the
brief nor the files NIKA puts on devices (startup scripts, configs, manifests,
image contents) may name the benchmark. Image names, Docker labels, and build
arguments stay on the host and are not checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nika.net_env.net_env_pool import (
    get_net_env_instance,
    list_all_net_envs,
    scenario_requires_topo_size,
    scenario_supported_backends,
)

pytestmark = pytest.mark.contract

# Match benchmark identifiers, including NikaAgent and nika-prefixed paths,
# while preserving real topology names such as Thessalonika and Carnikava.
_HOST_NAME = re.compile(r"(?<![a-z])nika", re.IGNORECASE)
_NET_ENV = Path(__file__).resolve().parents[3] / "src" / "nika" / "net_env"
# Dockerfile lines that never reach the image filesystem.
_HOST_ONLY_DOCKERFILE_LINE = re.compile(r"^\s*(#|FROM |ARG |LABEL )")


def test_device_files_do_not_name_the_host() -> None:
    leaks = []
    for path in _NET_ENV.rglob("*"):
        if not path.is_file() or path.suffix in {".py", ".pyc"}:
            continue
        # Topology source datasets and documentation stay on the host.
        # Agent briefs derived from them are checked below.
        if path.is_relative_to(_NET_ENV / "isp" / "topologies"):
            continue
        # lab.conf only selects images on the host.
        if path.name == "lab.conf":
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if path.name.startswith("Dockerfile"):
            lines = [
                line for line in lines if not _HOST_ONLY_DOCKERFILE_LINE.match(line)
            ]
        if any(_HOST_NAME.search(line) for line in lines):
            leaks.append(str(path.relative_to(_NET_ENV)))
    assert leaks == []


@pytest.mark.parametrize(
    "text",
    [
        "NIKA",
        "NikaAgent",
        "nika-proxy-token",
        "/opt/nika",
        "run_nika",
        "nika-agent.yaml",
    ],
)
def test_name_check_rejects_benchmark_identifiers(text: str) -> None:
    assert _HOST_NAME.search(text)


@pytest.mark.parametrize("text", ["Thessalonika", "Carnikava", "thessalonika_1"])
def test_name_check_preserves_real_topology_names(text: str) -> None:
    assert not _HOST_NAME.search(text)


@pytest.mark.parametrize("scenario", sorted(list_all_net_envs()))
def test_agent_brief_does_not_name_the_host(scenario: str) -> None:
    size = {"topo_size": "s"} if scenario_requires_topo_size(scenario) else {}
    for backend in scenario_supported_backends(scenario):
        brief = get_net_env_instance(scenario, backend=backend, **size).get_info()
        assert not _HOST_NAME.search(brief), (scenario, backend)
