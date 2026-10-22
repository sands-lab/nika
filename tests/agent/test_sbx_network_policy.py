"""Per-sandbox sbx network lockdown: global allow rules are denied per sandbox."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nika.agents.registry import get_agent_spec
from nika.agents.sandbox.config import resolve_sandbox_config
from nika.agents.sandbox.sbx.backend import SbxBackend
from nika.agents.sandbox.sbx.policy import (
    allowed_network_resources,
    resource_matches_host,
    sandbox_network_lockdown,
)

pytestmark = pytest.mark.unit

_SANDBOX = "agent-s1"

# Trimmed ``sbx policy ls --json`` output from a host with the balanced preset.
_POLICY = {
    "rules": [
        {
            "name": "default-code-and-containers",
            "applies_to": "all",
            "resource_type": "network",
            "decision": "allow",
            "resources": ["github.com:443", "**.githubusercontent.com:443"],
        },
        {
            "name": "default-package-managers",
            "applies_to": "all",
            "resource_type": "network",
            "decision": "allow",
            "resources": [
                "registry.npmjs.org:443",
                "pypi.org:443",
                "files.pythonhosted.org:443",
            ],
        },
        {
            "name": "default-ai-services",
            "applies_to": "all",
            "resource_type": "network",
            "decision": "allow",
            "resources": ["api.anthropic.com:443", "**.openai.com:443"],
        },
        {
            "name": "default-fs-read-allow-all",
            "applies_to": "all",
            "resource_type": "filesystem:read",
            "decision": "allow",
            "resources": ["**"],
        },
        {
            "name": "kit:agent-s1",
            "applies_to": f"sandbox:{_SANDBOX}",
            "resource_type": "network",
            "decision": "allow",
            "resources": ["platform.claude.com:443"],
        },
        {
            "name": "kit:agent-other",
            "applies_to": "sandbox:agent-other",
            "resource_type": "network",
            "decision": "allow",
            "resources": ["other.example.com:443"],
        },
        {
            "name": "peer-deny",
            "applies_to": "all",
            "resource_type": "network",
            "decision": "deny",
            "resources": ["evil.example.com:443"],
        },
        {
            "name": "user-rule",
            "applies_to": "all",
            "resource_type": "network",
            "decision": "allow",
            "resources": ["localhost:41000", "pypi.org"],
        },
    ]
}


def test_allowed_network_resources_covers_global_and_own_rules() -> None:
    assert allowed_network_resources(_POLICY, _SANDBOX) == [
        "github.com:443",
        "**.githubusercontent.com:443",
        "registry.npmjs.org:443",
        "pypi.org:443",
        "files.pythonhosted.org:443",
        "api.anthropic.com:443",
        "**.openai.com:443",
        "platform.claude.com:443",
        "localhost:41000",
        "pypi.org",
    ]


@pytest.mark.parametrize(
    ("resource", "host", "expected"),
    [
        ("**.githubusercontent.com:443", "objects.githubusercontent.com", True),
        ("**.githubusercontent.com:443", "githubusercontent.com", False),
        ("github.com:443", "github.com", True),
        ("github.com:443", "api.github.com", False),
        ("crl*.digicert.com:80", "crl3.digicert.com", True),
        ("pypi.org", "pypi.org:443", True),
        ("registry.npmjs.org:443", "pypi.org", False),
    ],
)
def test_resource_matches_host(resource: str, host: str, expected: bool) -> None:
    assert resource_matches_host(resource, [host]) is expected


def _policy_ls(*_args, **_kwargs):
    return SimpleNamespace(stdout=json.dumps(_POLICY))


def test_lockdown_splits_install_resources_and_keeps_egress() -> None:
    with patch(
        "nika.agents.sandbox.sbx.policy.run_sbx_checked", side_effect=_policy_ls
    ):
        rest, install = sandbox_network_lockdown(
            _SANDBOX,
            keep_hosts=("api.openai.com",),
            keep_resources=("localhost:41000",),
            install_pypi=True,
        )
    assert install == ["pypi.org:443", "files.pythonhosted.org:443", "pypi.org"]
    assert "github.com:443" in rest
    assert "**.githubusercontent.com:443" in rest
    assert "platform.claude.com:443" in rest
    # Egress host and the model proxy stay reachable.
    assert "**.openai.com:443" not in rest
    assert "localhost:41000" not in rest + install


def test_create_denies_global_allows_per_sandbox(tmp_path) -> None:
    backend = SbxBackend(
        replace(resolve_sandbox_config(), backend="sbx", offline_sdk_wheels=False)
    )
    backend._model_proxy_port = 41000
    spec = replace(get_agent_spec("byo.langgraph"), egress=("api.openai.com",))
    with (
        patch("nika.agents.sandbox.sbx.backend.get_agent_spec", return_value=spec),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_checked"),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_optional"),
        patch("nika.agents.sandbox.sbx.backend.install_agent_packages_in_sandbox"),
        patch(
            "nika.agents.sandbox.sbx.policy.run_sbx_checked", side_effect=_policy_ls
        ) as policy_checked,
        patch("nika.agents.sandbox.sbx.policy.run_sbx_optional") as policy_optional,
    ):
        name = backend.create(
            agent_session_id="s1", agent_type="byo.langgraph", workspace_dir=tmp_path
        )

    assert name == _SANDBOX
    calls = [list(c.args[0]) for c in policy_checked.call_args_list]
    assert ["policy", "ls", "--json"] in calls
    assert ["policy", "allow", "network", "--sandbox", name, "api.openai.com"] in calls
    assert ["policy", "allow", "network", "--sandbox", name, "pypi.org"] in calls
    denies = [c for c in calls if c[:2] == ["policy", "deny"]]
    assert [c[:5] for c in denies] == [
        ["policy", "deny", "network", "--sandbox", name]
    ] * 2
    # Everything else first, package indexes last (after the install).
    assert "github.com:443" in denies[0][5].split(",")
    assert denies[1][5] == "pypi.org:443,files.pythonhosted.org:443,pypi.org"
    revoked = [list(c.args[0]) for c in policy_optional.call_args_list]
    assert [
        "policy",
        "rm",
        "network",
        "--force",
        "--sandbox",
        name,
        "--resource",
        "pypi.org",
    ] in revoked
    # NIKA no longer adds global rules.
    assert all(
        "--sandbox" in c
        for c in calls
        if c[:2] in (["policy", "allow"], ["policy", "deny"])
    )


def test_create_removes_sandbox_when_lockdown_fails(tmp_path) -> None:
    backend = SbxBackend(
        replace(resolve_sandbox_config(), backend="sbx", keep_container=False)
    )
    with (
        patch("nika.agents.sandbox.sbx.backend.run_sbx_checked"),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_optional") as optional,
        patch("nika.agents.sandbox.sbx.backend.install_agent_packages_in_sandbox"),
        patch(
            "nika.agents.sandbox.sbx.policy.run_sbx_checked",
            side_effect=RuntimeError("policy ls failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="policy ls failed"):
            backend.create(
                agent_session_id="s1",
                agent_type="byo.langgraph",
                workspace_dir=tmp_path,
            )
    assert optional.call_args_list[-1].args[0] == ["rm", "--force", _SANDBOX]
