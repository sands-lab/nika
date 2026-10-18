"""Tests for Kathara runtime patches."""

from __future__ import annotations

import pytest

import nika.runtime.kathara.patch as kathara_patch

pytestmark = pytest.mark.unit


class KatharaPrivilegedPatchTest:
    def test_patch_marks_docker_machine_create(self) -> None:
        from Kathara.manager.docker.DockerMachine import DockerMachine

        kathara_patch.patch_kathara_privileged_without_root()
        assert getattr(DockerMachine.create, "_nika_priv_without_root", False)

    def test_allow_when_docker_reachable_and_not_root(self, monkeypatch) -> None:
        monkeypatch.setattr(kathara_patch, "docker_engine_reachable", lambda: True)

        assert kathara_patch.allow_privileged_without_root(is_admin=False) is True
        assert kathara_patch.allow_privileged_without_root(is_admin=True) is False

    def test_no_docker_disables_bypass(self, monkeypatch) -> None:
        monkeypatch.setattr(kathara_patch, "docker_engine_reachable", lambda: False)

        assert kathara_patch.allow_privileged_without_root(is_admin=False) is False
