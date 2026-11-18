import time

import pytest

from nika.net_env.base import NetworkEnvBase

pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self, deployed: bool) -> None:
        self._deployed = deployed

    def deploy(self) -> bool:
        return self._deployed


class _BareEnv(NetworkEnvBase):
    def _ensure_runtime_files(self) -> None:
        return None

    def _build_runtime(self):
        return _Runtime(True)


class _StartupEnv(_BareEnv):
    def startup_verify_lab(self) -> dict:
        return {"verified": True}


class _VerifyEnv(_BareEnv):
    def verify_lab(self) -> dict:
        return {"verified": True}


def test_deploy_skips_settle_when_startup_verify_exists(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _StartupEnv().deploy()
    assert slept == []


def test_deploy_skips_settle_when_verify_lab_exists(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _VerifyEnv().deploy()
    assert slept == []


def test_deploy_settles_without_readiness_verifier(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _BareEnv().deploy()
    assert slept == [5.0]


def test_deploy_skips_settle_when_lab_already_exists(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    class Existing(_BareEnv):
        def _build_runtime(self):
            return _Runtime(False)

    Existing().deploy()
    assert slept == []
