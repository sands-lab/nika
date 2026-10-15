from __future__ import annotations

import sys

import pytest

from collections.abc import Callable
from typing import TypeVar
from unittest.mock import patch

T = TypeVar("T")


def ready_node_count(output: str) -> int:
    ready = 0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "Ready":
            ready += 1
    return ready


def instantiate_with_mocked_kathara(
    patch_target: str,
    factory: Callable[[], T],
) -> T:
    with patch(patch_target, return_value=object()):
        return factory()


def assert_verify_success(result: dict) -> None:
    assert result["verified"], result["checks"]
    assert all(result["checks"].values()), result["checks"]


# Unit scenario fixtures supply observations; the batch transport itself is
# exercised with a real shell in test_verify_collection.py.


@pytest.fixture
def stub_verify_collection(monkeypatch):
    from nika.net_env.verify_collection import (
        CommandResult,
        VerifyCollection,
        collect_verify_commands,
    )
    from nika.runtime.base import LabRuntime

    def collect(runtime, commands, *, nodes=None):
        if isinstance(runtime, (LabRuntime, VerifyCollection)):
            return collect_verify_commands(runtime, commands, nodes=nodes)
        results = {}
        for command in commands:
            key = command.node, command.command
            if key not in results:
                output = runtime.exec(
                    command.node, command.command, timeout=command.timeout
                )
                error = output if output.startswith("[TIMEOUT]") else None
                results[key] = CommandResult(
                    output, 124 if error else 0, error, required=command.required
                )
        if nodes is None:
            nodes = (
                runtime.list_nodes()
                if hasattr(runtime, "list_nodes")
                else sorted({command.node for command in commands})
            )
        return VerifyCollection(results, nodes, 0)

    for name, module in list(sys.modules.items()):
        if (
            name.startswith("nika.net_env.")
            and getattr(module, "collect_verify_commands", None)
            is collect_verify_commands
        ):
            monkeypatch.setattr(module, "collect_verify_commands", collect)
