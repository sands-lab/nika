"""Only a verified startup boundary removes Kubernetes bootstrap warnings."""

from types import SimpleNamespace

import pytest

from nika.net_env.utils.k8s_traffic_pods import K8sTrafficPods

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("healthy", [True, False])
def test_bootstrap_warnings_require_current_health(monkeypatch, healthy):
    actions = []

    class Parent:
        SEAL_BASELINE = True

        def seal_baseline(self):
            actions.append("sources_sealed")

    class Environment(K8sTrafficPods, Parent):
        def _build_runtime(self):
            def execute(node, command, **kwargs):
                assert node == "controller"
                if "wait -A" in command:
                    actions.append("pods_ready")
                    return "__rc=0"
                assert "delete events" in command
                assert "type=Warning" in command
                actions.append("warnings_cleared")
                return "__rc=0"

            return SimpleNamespace(exec=execute)

    def verify(env):
        actions.append("health_checked")
        if not healthy:
            raise RuntimeError("serving pods are not ready")

    monkeypatch.setattr("nika.net_env.verify.verify_lab_with_retry", verify)
    env = Environment()
    if healthy:
        env.seal_baseline()
        assert actions == [
            "health_checked",
            "pods_ready",
            "sources_sealed",
            "warnings_cleared",
        ]
    else:
        with pytest.raises(RuntimeError, match="serving pods"):
            env.seal_baseline()
        assert actions == ["health_checked"]


@pytest.mark.parametrize(
    "enabled,healthy", [(True, True), (True, False), (False, True)]
)
@pytest.mark.parametrize("scenario", ["k8s_lab", "llmd_lab"])
def test_removes_runtime_manifests_after_parent_sealing(
    monkeypatch, enabled, healthy, scenario
):
    from nika.net_env.k8s_lab.lab import K8sFatTreeBGP
    from nika.net_env.llmd_lab.lab import LLMDInferenceCluster
    from nika.service.lab import checked_exec as checked_module

    actions = []

    def seal_parent(self):
        actions.append("parent_sealed")
        if not healthy:
            raise RuntimeError("cluster is not ready")

    monkeypatch.setattr(K8sTrafficPods, "seal_baseline", seal_parent)
    original_checked = checked_module.checked_exec

    def check_scope(execute, node, command, **kwargs):
        assert command == (
            "rm -rf /shared /k8s"
            if scenario == "k8s_lab"
            else "rm -f /k8s/*.yaml /k8s/*.yml"
        )
        return original_checked(execute, node, command, **kwargs)

    monkeypatch.setattr(checked_module, "checked_exec", check_scope)

    def execute(node, command, **kwargs):
        assert node == "controller"
        actions.append("runtime_manifests_removed")
        return "__rc=0"

    env = object.__new__(
        K8sFatTreeBGP if scenario == "k8s_lab" else LLMDInferenceCluster
    )
    env.SEAL_BASELINE = enabled
    monkeypatch.setattr(env, "_build_runtime", lambda: SimpleNamespace(exec=execute))
    if enabled and not healthy:
        with pytest.raises(RuntimeError, match="not ready"):
            env.seal_baseline()
        assert actions == ["parent_sealed"]
        return
    env.seal_baseline()
    assert actions == (
        ["parent_sealed", "runtime_manifests_removed"] if enabled else []
    )


def test_unready_pod_preserves_warning_history(monkeypatch):
    from unittest.mock import Mock
    from nika.net_env.base import NetworkEnvBase
    from nika.net_env.k8s_lab.lab import K8sFatTreeBGP

    env = object.__new__(K8sFatTreeBGP)
    runtime = Mock()
    runtime.exec.return_value = "Pod readiness timed out\n__rc=1"
    monkeypatch.setattr(env, "_build_runtime", lambda: runtime)
    monkeypatch.setattr("nika.net_env.verify.verify_lab_with_retry", lambda env: None)
    monkeypatch.setattr(NetworkEnvBase, "seal_baseline", lambda self: None)
    with pytest.raises(RuntimeError, match="Pod readiness timed out"):
        env.seal_baseline()
    assert not any(
        "delete events" in call.args[1] for call in runtime.exec.call_args_list
    )
