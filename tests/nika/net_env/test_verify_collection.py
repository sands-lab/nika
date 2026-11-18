"""Verification batches exercise a real shell and retain per-command failures."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nika.net_env.verify_collection import VerifyCommand, collect_verify_commands

pytestmark = pytest.mark.unit


class _Shell:
    def __init__(self):
        self.calls = 0

    def list_nodes(self):
        return ["node"]

    def exec(self, node, command, *, timeout=10):
        self.calls += 1
        return subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout.strip()


def test_batch_preserves_multiline_stderr_and_nonzero_results():
    runtime = _Shell()
    first = VerifyCommand(
        "node", "printf 'one\\ntwo\\n'; printf 'stderr\\n' >&2; exit 1"
    )
    second = VerifyCommand("node", "printf 'still collected'")
    empty = VerifyCommand("node", "true")
    collected = collect_verify_commands(runtime, [first, second, empty])
    assert runtime.calls == 1
    assert collected.exec("node", first.command) == "one\ntwo\nstderr"
    assert collected.results[("node", first.command)].returncode == 1
    assert collected.exec("node", second.command) == "still collected"
    assert collected.exec("node", empty.command) == ""
    assert collected.results[("node", empty.command)].returncode == 0
    assert not collected.errors


def test_batch_timeout_does_not_hide_other_evidence():
    runtime = _Shell()
    slow = VerifyCommand("node", "sleep 2", 0.05)
    healthy = VerifyCommand("node", "printf healthy")
    collected = collect_verify_commands(runtime, [slow, healthy])
    assert collected.exec("node", healthy.command) == "healthy"
    assert collected.results[("node", slow.command)].returncode == 124
    assert collected.errors
    result = collected.finish(
        {"verified": True, "checks": {"isolated": True}, "details": {}}
    )
    assert not result["verified"]
    assert not result["checks"]["observations_collected"]


def test_batch_deduplicates_within_phase_but_refreshes_after_change(tmp_path: Path):
    runtime = _Shell()
    path = tmp_path / "observations"
    command = VerifyCommand("node", f"printf x >> '{path}'; cat '{path}'")
    first = collect_verify_commands(runtime, [command, command])
    assert path.read_text() == "x"
    assert first.exec("node", command.command) == "x"
    assert runtime.calls == 1
    second = collect_verify_commands(runtime, [command])
    assert first.exec("node", command.command) == "x"
    assert second.exec("node", command.command) == "xx"
    assert first.merged(second).exec("node", command.command) == "xx"


def test_unplanned_read_cannot_make_isolation_pass():
    collected = collect_verify_commands(_Shell(), [])
    with pytest.raises(ValueError, match="uncollected"):
        collected.exec("node", "missing")
    assert not collected.finish({"verified": True, "checks": {}, "details": {}})[
        "verified"
    ]


def test_optional_timeout_is_reported_without_gating_required_checks():
    request = VerifyCommand("node", "sleep 2", 0.05, required=False)
    collected = collect_verify_commands(_Shell(), [request])
    assert collected.errors
    assert not collected.blocking_errors
    result = collected.finish(
        {"verified": True, "checks": {"required": True}, "details": {}}
    )
    assert result["verified"]


def test_truncated_output_retains_complete_evidence_and_fails_collection():
    class TruncatedShell(_Shell):
        def exec(self, node, command, *, timeout=10):
            return super().exec(node, command, timeout=timeout).rsplit("END ", 1)[0]

    healthy = VerifyCommand("node", "printf healthy")
    missing = VerifyCommand("node", "printf incomplete")
    collected = collect_verify_commands(TruncatedShell(), [healthy, missing])
    assert collected.exec("node", healthy.command) == "healthy"
    assert (
        collected.results[("node", missing.command)].error == "incomplete batch output"
    )
    assert not collected.finish({"verified": True, "checks": {}, "details": {}})[
        "verified"
    ]


def test_invalid_json_is_reported_as_a_required_observation_error():
    request = VerifyCommand("node", "printf invalid-json")
    collected = collect_verify_commands(_Shell(), [request])
    with pytest.raises(ValueError):
        collected.json("node", request.command)
    assert collected.blocking_errors
    assert not collected.finish({"verified": True, "checks": {}, "details": {}})[
        "verified"
    ]


def test_large_node_collection_preserves_every_observation():
    runtime = _Shell()
    requests = [VerifyCommand("node", f"printf result-{index}") for index in range(400)]
    collected = collect_verify_commands(runtime, requests)
    assert runtime.calls > 1
    assert not collected.errors
    assert [collected.exec("node", request.command) for request in requests] == [
        f"result-{index}" for index in range(400)
    ]
