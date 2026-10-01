"""Real deployment regression for bootstrap errors and owned-resource cleanup."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import pytest

from nika.workflows.session.close import close_session
from nika.workflows.session.list import list_sessions
from tests.support.prerequisites import docker_available, privileged_lab_supported

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not (docker_available() and privileged_lab_supported()),
    reason="Requires privileged Docker labs",
)
def test_llmd_bootstrap_failure_reports_stage_and_cleans_session(
    tmp_path, monkeypatch
) -> None:
    from nika.net_env.llmd_lab.lab import LLMDInferenceCluster
    from nika.workflows.env.start import start_net_env

    instance_tag = f"bootstrap-failure-{uuid4().hex[:8]}"
    original_init = LLMDInferenceCluster.__init__
    environments = []

    def invalid_manifest(self, **kwargs):
        original_init(self, **kwargs)
        self.lab.machines["controller"].create_file_from_string(
            "apiVersion: [invalid\n", "/k8s/metallb.yaml"
        )
        environments.append(self)

    monkeypatch.setattr(LLMDInferenceCluster, "__init__", invalid_manifest)
    monkeypatch.setattr(LLMDInferenceCluster, "VERIFY_MAX_WAIT_SEC", 60)
    try:
        with pytest.raises(
            RuntimeError, match="bootstrap failed: apply MetalLB failed"
        ):
            start_net_env(
                "llmd_lab", None, instance_tag=instance_tag, result_dir=str(tmp_path)
            )
        assert not any(
            instance_tag in str(row.get("lab_name"))
            for row in list_sessions(running_only=True)
        )
        assert environments and not environments[0].lab_exists()
        events = [
            json.loads(line)
            for path in tmp_path.rglob("nika.jsonl")
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        failure = next(
            event for event in events if event.get("event") == "env_verify_failed"
        )
        assert "error converting YAML to JSON" in failure["data"]["error"]
        preload = next(
            event
            for event in events
            if event.get("event") == "env_preload_progress"
            and event["message"].startswith("preload complete")
        )
        failure_delay = (
            datetime.fromisoformat(failure["timestamp"])
            - datetime.fromisoformat(preload["timestamp"])
        ).total_seconds()
        assert failure_delay < 60, (
            f"Bootstrap failure took {failure_delay:.1f}s to surface"
        )
    finally:
        for row in list_sessions(running_only=True):
            if instance_tag in str(row.get("lab_name") or ""):
                close_session(session_id=row["session_id"], undeploy=True)
