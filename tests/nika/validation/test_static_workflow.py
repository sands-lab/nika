from __future__ import annotations

from types import SimpleNamespace

from nika.net_env.contract import ValidationContract, ValidationReport
from nika.validation.base import ValidationSnapshot
from nika.workflows.validation.static import run_static_validation


def _contract(scenario: str) -> ValidationContract:
    return ValidationContract(
        contract_id=f"{scenario}.baseline",
        scenario=scenario,
        design_source={},
        intents=(),
    )


def test_unsupported_environment_skips_static_validation(tmp_path) -> None:
    reports = run_static_validation(
        net_env=SimpleNamespace(backend="containerlab", device_profile="frr"),
        contract=_contract("isp_abilene"),
        artifact_dir=tmp_path,
    )
    assert reports == {}
    assert list(tmp_path.iterdir()) == []


def test_supported_environment_uses_internal_batfish_defaults(
    tmp_path, monkeypatch
) -> None:
    import nika.workflows.validation.static as workflow

    service_calls = []
    verifier_calls = []
    snapshot = ValidationSnapshot(
        snapshot_id="snapshot-id",
        path=tmp_path / "snapshot",
        metadata={},
    )

    def fake_service(*, host, port):
        service_calls.append((host, port))

    class FakeVerifier:
        name = "batfish"
        supported_properties = frozenset()

        def __init__(self, *, host, port):
            verifier_calls.append((host, port))

        def verify(self, contract, received_snapshot):
            assert received_snapshot == snapshot
            return ValidationReport.from_results(contract, self.name, ())

    monkeypatch.setattr(workflow, "ensure_batfish_service", fake_service)
    monkeypatch.setattr(workflow, "build_isp_snapshot", lambda **_kwargs: snapshot)
    monkeypatch.setattr(workflow, "BatfishVerifier", FakeVerifier)

    env = SimpleNamespace(
        backend="kathara",
        device_profile="frr",
        plan=object(),
        traffic=object(),
        deployment_configs={},
    )
    reports = run_static_validation(
        net_env=env,
        contract=_contract("isp_abilene"),
        artifact_dir=tmp_path,
    )

    assert service_calls == [("127.0.0.1", 9996)]
    assert verifier_calls == [("127.0.0.1", 9996)]
    assert reports["batfish"].status == "passed"
    assert (tmp_path / "batfish-validation.json").is_file()
