"""Failure edits retain the configuration's original modification time."""

import os
import subprocess

import pytest

from nika.problems.support.file_metadata import preserve_mtime

pytestmark = pytest.mark.unit


class ShellRuntime:
    def exec(self, node, command):
        return subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True, check=True
        ).stdout


@pytest.mark.parametrize("edit_fails", [False, True])
def test_edit_preserves_nanosecond_mtime_even_on_failure(tmp_path, edit_fails):
    path = tmp_path / "resolver 'config' $(false)"
    path.write_text("healthy\n")
    epoch_ns = 1700000000123456789
    os.utime(path, ns=(epoch_ns, epoch_ns))

    def edit():
        with preserve_mtime(ShellRuntime(), "local", str(path)):
            # sed -i replaces the inode; exercise that kind of config edit.
            replacement = tmp_path / "replacement"
            replacement.write_text("fault\n")
            replacement.replace(path)
            if edit_fails:
                raise ValueError("partial edit")

    if edit_fails:
        with pytest.raises(ValueError, match="partial edit"):
            edit()
    else:
        edit()
    assert path.read_text() == "fault\n"
    assert path.stat().st_mtime_ns == epoch_ns
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
