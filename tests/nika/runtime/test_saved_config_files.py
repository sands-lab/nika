"""Saved-file normalization preserves configuration and refreshes every peer."""

from __future__ import annotations

import os
import shlex
import subprocess

import pytest

from nika.runtime.shared.config_files import rewrite_saved_config_files

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "pattern,names",
    [("*.conf", ["frr.conf", "bgpd.conf"]), ("config.json", ["config.json"])],
)
def test_saved_config_refreshes_all_peers_without_changing_content(
    tmp_path, pattern, names
):
    peers = [tmp_path / "peer1", tmp_path / "peer2"]
    before = {}
    for peer in peers:
        (peer / "config").mkdir(parents=True)
        for name in names:
            path = peer / "config" / name
            path.write_text(f"{peer.name}: {name}\n")
            path.chmod(0o640)
            os.utime(path, (100, 100 if peer == peers[0] else 200))
            before[path] = (path.read_bytes(), path.stat().st_ino)

    def execute(peer, command):
        subprocess.run(
            ["sh", "-c", f"cd {shlex.quote(str(peer))}; {command}"],
            check=True,
            capture_output=True,
        )

    rewrite_saved_config_files(execute, peers, "config", pattern, 300)
    for path, (content, inode) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == 0o640
        assert path.stat().st_mtime == 300
        assert path.stat().st_ino != inode
    for peer in peers:
        assert sorted(p.name for p in (peer / "config").iterdir()) == sorted(names)


def test_missing_saved_files_do_not_create_placeholders(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    rewrite_saved_config_files(
        lambda node, command: subprocess.run(
            ["sh", "-c", command], check=True, capture_output=True
        ),
        ["peer"],
        str(directory),
        "*.conf",
        300,
    )
    assert list(directory.iterdir()) == []
