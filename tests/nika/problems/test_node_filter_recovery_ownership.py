"""Identical later rules and failed acknowledgements retain their owners."""

import shlex
from types import SimpleNamespace

import pytest

from nika.problems.support.kubernetes.node_filter import (
    DropSpec,
    NodeFilter,
    NodeFilterError,
)

pytestmark = pytest.mark.unit


class _Rules:
    def __init__(self):
        self.rules = []
        self.deletes = 0
        self.fail_readback = False
        self.lose_delete_ack = False

    def execute(self, command):
        words = shlex.split(command)
        operation = next(word for word in words if word in ("-C", "-I", "-D", "-S"))
        table = words[words.index("-t") + 1]
        index = words.index(operation)
        chain = words[index + 1]
        if operation == "-S":
            if self.deletes and self.fail_readback:
                self.fail_readback = False
                raise NodeFilterError("Readback failed")
            return "\n".join(
                f"-A {chain} {rule}"
                for rule_table, rule_chain, rule in self.rules
                if (rule_table, rule_chain) == (table, chain)
            ), 0
        rest = words[index + (3 if operation == "-I" else 2) :]
        key = (table, chain, " ".join(word for word in rest if word != "2>/dev/null"))
        if operation == "-C":
            return "", 0 if key in self.rules else 1
        if operation == "-I":
            self.rules.insert(0, key)
        else:
            self.rules.remove(key)
            self.deletes += 1
            if self.lose_delete_ack:
                self.lose_delete_ack = False
                raise NodeFilterError("Deletion acknowledgement lost")
        return "", 0


@pytest.mark.parametrize("failure", [None, "fail_readback", "lose_delete_ack"])
def test_recovery_keeps_later_identical_rule_after_retry(failure):
    runtime = _Rules()
    node_filter = NodeFilter(None, "worker")
    node_filter._exec = runtime.execute
    spec = DropSpec("10.0.0.1", "tcp", 6443)
    node_filter._install_rule("raw", "OUTPUT", spec)
    runtime.rules.append(runtime.rules[0])
    if failure:
        setattr(runtime, failure, True)
        with pytest.raises(NodeFilterError):
            node_filter.recover()
    assert node_filter.recover()
    assert node_filter.recover()
    assert runtime.rules == [("raw", "OUTPUT", spec.iptables_args())]
    assert runtime.deletes == 1


def test_install_refuses_failed_presence_check():
    node_filter = NodeFilter(None, "worker")
    calls = []

    def execute(command):
        calls.append(command)
        return "xtables lock unavailable", 4

    node_filter._exec = execute
    with pytest.raises(NodeFilterError, match="Could not inspect"):
        node_filter._install_rule("raw", "OUTPUT", DropSpec("10.0.0.1"))
    assert len(calls) == 1
    assert node_filter.recover()


def test_invalid_exit_code_is_not_treated_as_rule_absence():
    runtime = SimpleNamespace(exec=lambda *args, **kwargs: "__rc=invalid\n")
    node_filter = NodeFilter(runtime, "worker")
    with pytest.raises(NodeFilterError, match="Invalid command exit-code"):
        node_filter._install_rule("raw", "OUTPUT", DropSpec("10.0.0.1"))


def test_recovery_preserves_preexisting_drop():
    rules = _Rules()
    spec = DropSpec("10.0.0.1", "tcp", 6443)
    original = ("raw", "OUTPUT", spec.iptables_args())
    rules.rules.append(original)
    node_filter = NodeFilter(None, "worker")
    node_filter._exec = rules.execute
    node_filter.block(spec)
    assert node_filter.recover()
    assert rules.rules == [original]
