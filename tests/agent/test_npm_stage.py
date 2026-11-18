from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nika.agents.registry import get_agent_spec
from nika.agents.sandbox.npm import (
    NPM_DIRNAME,
    npm_bin_dir,
    stage_npm,
)

pytestmark = pytest.mark.unit


def test_stage_npm_skips_empty_packages(tmp_path: Path) -> None:
    assert stage_npm(tmp_path, ()) is None
    assert not (tmp_path / NPM_DIRNAME).exists()


def test_stage_npm_copies_host_prefix(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("nika.agents.sandbox.npm._NPM_CACHE", cache_root)

    def fake_install(dest: Path, packages: tuple[str, ...]) -> None:
        lib = dest / "lib" / "node_modules" / "cli" / "lib"
        lib.mkdir(parents=True)
        (lib / "bin.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
        bin_dir = dest / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "dsh").symlink_to("../lib/node_modules/cli/lib/bin.js")
        (dest / ".packages.txt").write_text(
            "\n".join(packages) + "\n", encoding="utf-8"
        )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with patch("nika.agents.sandbox.npm._npm_install", side_effect=fake_install):
        prefix = stage_npm(workspace, ("opencode-ai@1.19.3",))
    assert prefix == workspace / NPM_DIRNAME
    staged = npm_bin_dir(workspace) / "dsh"
    assert staged.is_symlink()
    assert staged.resolve().is_file()


def test_cli_harnesses_declare_pinned_npm_packages() -> None:
    opencode = get_agent_spec("cli.opencode")
    dsh = get_agent_spec("cli.dsh")
    assert opencode.template == "shell"
    assert dsh.template == "shell"
    assert opencode.npm_packages == ("opencode-ai@1.19.3",)
    assert dsh.npm_packages == ("@deepseek-ai/dsh@0.2.1-rc.2",)
