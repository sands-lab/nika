"""Injected artifacts must not name the tool or leave pre-fault copies on nodes."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

SRC = Path(__file__).resolve().parents[3] / "src" / "nika"
PROBLEMS = SRC / "problems"

# Node-side file paths under well-known roots whose name mentions the tool.
_NIKA_PATH = re.compile(
    r"(?:/tmp|/var/tmp|/var/run|/run|/etc|/opt|/root|/home)/\S*nika", re.IGNORECASE
)
# Runtime tooling shipped in the lab images, not written at injection time.
_ALLOWED = ("/opt/nika",)


def _string_literals(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_problem_artifact_paths_do_not_name_the_tool() -> None:
    offenders = [
        f"{path.relative_to(PROBLEMS)}: {match.group(0)}"
        for path in PROBLEMS.rglob("*.py")
        for literal in _string_literals(path)
        for match in _NIKA_PATH.finditer(literal)
        if not match.group(0).startswith(_ALLOWED)
    ]
    assert not offenders, offenders


# Lab and tooling code whose paths and names agents can read on a node. Host-side
# files (the containerlab host's flap pidfiles, the host state-directory hint in
# an error message) are not visible to agents.
_HOST_SIDE = {
    "service/containerlab/host_tc.py",
    "net_env/utils/k8s_workload_cache.py",
}
_NODE_TEXT = re.compile(
    r"(?:/tmp|/var/tmp|/var/run|/var/lib|/run|/etc|/opt|/root|/home|/usr)/\S*nika"
    r"|(?<![\w.])/nika[-_/\w]*|\[nika-|NIKA_STAGE",
    re.IGNORECASE,
)


def test_lab_and_service_node_paths_do_not_name_the_tool() -> None:
    roots = (SRC / "net_env", SRC / "service")
    sources = [
        (path, literal)
        for root in roots
        for path in root.rglob("*.py")
        for literal in _string_literals(path)
    ] + [
        (path, path.read_text())
        for root in roots
        for suffix in (".startup", ".sh", ".conf")
        for path in root.rglob(f"*{suffix}")
    ]
    offenders = [
        f"{path.relative_to(SRC)}: {match.group(0)}"
        for path, text in sources
        if path.relative_to(SRC).as_posix() not in _HOST_SIDE
        for match in _NODE_TEXT.finditer(text)
    ]
    assert not offenders, offenders


def test_frr_config_is_saved_only_through_the_shared_helper() -> None:
    # A bare ``write memory`` leaves frr.conf.sav, which diffs to the fault.
    offenders = [
        str(path.relative_to(PROBLEMS))
        for path in PROBLEMS.rglob("*.py")
        if path.name != "frr.py" and "write memory" in path.read_text()
    ]
    assert not offenders, offenders
