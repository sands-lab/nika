from __future__ import annotations

import pytest
import json
import tempfile
from pathlib import Path
from nika.runtime.factory import resolve_backend, runtime_for_session
from nika.utils.session import Session
from nika.utils.session_store import SessionStore


class BackendResolveTest:
    def test_legacy_session_defaults_to_kathara(self) -> None:
        meta = {
            "session_id": "legacy-1",
            "lab_name": "simple_bgp__abc123",
            "scenario_name": "simple_bgp",
            "scenario_params": {"lab_name": "simple_bgp__abc123"},
        }

        assert resolve_backend(meta) == "kathara"

    def test_session_backend_field(self) -> None:
        meta = {"backend": "containerlab", "scenario_params": {}}

        assert resolve_backend(meta) == "containerlab"

    def test_scenario_params_backend_fallback(self) -> None:
        meta = {"scenario_params": {"backend": "containerlab"}}

        assert resolve_backend(meta) == "containerlab"

    def test_containerlab_only_scenario_infers_backend(self) -> None:
        meta = {
            "session_id": "clab-1",
            "lab_name": "min3clos__abc123",
            "scenario_name": "min3clos",
            "scenario_params": {"lab_name": "min3clos__abc123"},
        }

        assert resolve_backend(meta) == "containerlab"

    def test_init_session_persists_backend_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_root = Path(tmp) / "results"
            sessions_dir = Path(tmp) / "runtime" / "sessions"
            db_path = Path(tmp) / "runtime" / "sessions.db"
            store = SessionStore(sessions_dir=sessions_dir, db_path=db_path)
            session = Session()
            session.store = store
            session.init_session(
                session_id="20260706-120000-abc123",
                scenario_name="min3clos",
                lab_name="min3clos__tag",
                scenario_topo_size=None,
                scenario_params={
                    "lab_name": "min3clos__tag",
                    "backend": "containerlab",
                },
                result_dir=results_root,
                backend="containerlab",
                topology_file="/tmp/topo.clab.yml",
                runtime_workdir="/tmp/runtime/clab",
            )
            stored = store.get_session("20260706-120000-abc123")

            assert stored["backend"] == "containerlab"

            assert stored["topology_file"] == "/tmp/topo.clab.yml"

            assert stored["runtime_workdir"] == "/tmp/runtime/clab"
            run_path = results_root / "20260706-120000-abc123" / "run.json"
            run_meta = json.loads(run_path.read_text(encoding="utf-8"))

            assert run_meta["backend"] == "containerlab"

    def test_runtime_for_session_containerlab_requires_topology(self) -> None:
        with pytest.raises(ValueError):
            runtime_for_session(
                {
                    "backend": "containerlab",
                    "lab_name": "min3clos__x",
                    "scenario_name": "min3clos",
                }
            )

    def test_start_net_env_infers_containerlab_for_min3clos(self) -> None:
        from unittest.mock import patch

        from nika.workflows.env.start import start_net_env

        with tempfile.TemporaryDirectory() as tmp:
            with patch("nika.workflows.env.start.get_net_env_instance") as mock_env:
                mock_env.return_value.lab_exists.return_value = True
                mock_env.return_value.name = "min3clos__tag"
                mock_env.return_value.deploy = lambda: None
                mock_env.return_value.undeploy = lambda: None
                with patch(
                    "nika.workflows.env.start.verify_lab_with_retry", return_value=None
                ):
                    with patch("nika.workflows.env.start.Session") as mock_session_cls:
                        session = mock_session_cls.return_value
                        # Real path required: MagicMock.session_dir is PathLike and
                        # would mkdir MagicMock/Session().session_dir/<id> under CWD.
                        session.session_dir = str(Path(tmp) / "session")
                        start_net_env("min3clos", None)
                        _, kwargs = mock_env.call_args

                        assert kwargs.get("backend") == "containerlab"
                        init_kwargs = session.init_session.call_args.kwargs

                        assert init_kwargs.get("backend") == "containerlab"

    def test_start_net_env_defaults_isp_to_kathara(self) -> None:
        from unittest.mock import patch

        from nika.workflows.env.start import start_net_env

        with tempfile.TemporaryDirectory() as tmp:
            with patch("nika.workflows.env.start.get_net_env_instance") as mock_env:
                mock_env.return_value.lab_exists.return_value = True
                mock_env.return_value.name = "isp__tag"
                mock_env.return_value.deploy = lambda: None
                mock_env.return_value.undeploy = lambda: None
                with patch(
                    "nika.workflows.env.start.verify_lab_with_retry", return_value=None
                ):
                    with patch("nika.workflows.env.start.Session") as mock_session_cls:
                        session = mock_session_cls.return_value
                        session.session_dir = str(Path(tmp) / "session")
                        start_net_env("isp_abilene", None)
                        _, kwargs = mock_env.call_args
                        assert kwargs.get("backend") == "kathara"
                        assert "topo_size" not in kwargs
                        init_kwargs = session.init_session.call_args.kwargs
                        assert init_kwargs.get("backend") == "kathara"
                        assert init_kwargs.get("scenario_topo_size") == "s"

    def test_runtime_for_fixed_isp_session_ignores_sampling_identity(self) -> None:
        runtime = runtime_for_session(
            {
                "backend": "kathara",
                "lab_name": "isp_abilene__saved",
                "scenario_name": "isp_abilene",
                "scenario_params": {
                    "backend": "kathara",
                    "lab_name": "isp_abilene__saved",
                    "topo_size": "s",
                    "topo": "abilene",
                    "igp": "ospf",
                    "bgp_mode": "ebgp",
                    "device_profile": "frr",
                },
            }
        )

        assert runtime._net_env.scenario_id == "isp_abilene"
        assert runtime._net_env.name == "isp_abilene__saved"

    def test_start_net_env_isp_containerlab(self) -> None:
        from unittest.mock import patch

        from nika.workflows.env.start import start_net_env

        with tempfile.TemporaryDirectory() as tmp:
            with patch("nika.workflows.env.start.get_net_env_instance") as mock_env:
                mock_env.return_value.lab_exists.return_value = True
                mock_env.return_value.name = "isp__tag"
                mock_env.return_value.deploy = lambda: None
                mock_env.return_value.undeploy = lambda: None
                mock_env.return_value._ensure_runtime_files = lambda: None
                with patch(
                    "nika.workflows.env.start.verify_lab_with_retry", return_value=None
                ):
                    with patch("nika.workflows.env.start.Session") as mock_session_cls:
                        session = mock_session_cls.return_value
                        session.session_dir = str(Path(tmp) / "session")
                        start_net_env("isp_abilene", None, backend="containerlab")
                        _, kwargs = mock_env.call_args
                        assert kwargs.get("backend") == "containerlab"
                        init_kwargs = session.init_session.call_args.kwargs
                        assert init_kwargs.get("backend") == "containerlab"
