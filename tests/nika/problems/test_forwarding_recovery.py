"""Packet-filter recovery preserves rules owned by other faults."""

import json

import pytest

from nika.problems.support.nft_recovery import NftDropRecovery

pytestmark = pytest.mark.unit


class NftRuntime:
    def __init__(self):
        self.handles = {1}
        self.fail_add = False

    def exec(self, node, command):
        if "delete rule" in command:
            self.handles.remove(int(command.split()[-1]))
            return ""
        return json.dumps(
            {
                "nftables": [
                    {
                        "rule": {
                            "family": "inet",
                            "table": "filter",
                            "chain": "input",
                            "handle": handle,
                        }
                    }
                    for handle in self.handles
                ]
            }
        )

    def add_nft_drop_rule(self, node, rule, *, family):
        self.handles.add(max(self.handles) + 1)
        if self.fail_add:
            raise RuntimeError("partial add")


@pytest.mark.parametrize("partial", [False, True])
def test_nft_recovery_preserves_existing_and_later_rules(partial):
    runtime = NftRuntime()
    recovery = NftDropRecovery()
    runtime.fail_add = partial
    if partial:
        with pytest.raises(RuntimeError, match="partial add"):
            recovery.add(runtime, "host", "drop")
    else:
        recovery.add(runtime, "host", "drop")
    runtime.handles.add(99)
    assert recovery.restore(runtime)
    assert recovery.restore(runtime)
    assert runtime.handles == {1, 99}


def test_nft_recovery_reports_incomplete_tracking_after_failed_read(monkeypatch):
    runtime = NftRuntime()
    recovery = NftDropRecovery()
    read = recovery._handles
    calls = 0

    def handles(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("ruleset read failed")
        return read(*args)

    monkeypatch.setattr(recovery, "_handles", handles)
    with pytest.raises(RuntimeError, match="ruleset read failed"):
        recovery.add(runtime, "host", "drop")
    assert runtime.handles == {1, 2}
    assert recovery.restore(runtime) is False
    assert runtime.handles == {1, 2}
