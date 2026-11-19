"""Image preparation prunes NIKA builds while preserving operator images."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nika.workflows.setup import images

pytestmark = pytest.mark.unit


def test_stale_cleanup_preserves_vendor_and_upstream_images(monkeypatch):
    def image(name, *, digest=False, label=False):
        return SimpleNamespace(
            id=f"id-{name}",
            tags=[] if digest else [name],
            attrs={"RepoDigests": [name] if digest else []},
            labels={images.IMAGE_LABEL: "nika/base:old"} if label else {},
        )

    vendor = [
        "ios-xr/xrd-control-plane:26.2.1",
        "vrnetlab/mikrotik_routeros:7.21.5",
        "ghcr.io/nokia/srlinux:24.10",
        "docker-sonic-vs:old",
        "rancher/k3s:old",
        "quay.io/metallb/controller:old",
        "ghcr.io/llm-d/llm-d:old",
        "nlnetlabs/routinator:old",
        "batfish/batfish:old",
        "cr.agentgateway.dev/agentgateway:old",
        "ik2227/host:old",
    ]
    tagged = [image(name) for name in vendor]
    untagged = [
        image(name.split(":")[0] + "@sha256:1234", digest=True) for name in vendor
    ]
    stale = image("nika/base:old")
    legacy = image("kathara/nika-base:latest")
    active = image("nika/frr:old")
    current = image("nika/base:current")
    dangling = image("dangling", label=True)
    dangling.tags = []
    # A downstream image may inherit NIKA's build label and keep a vendor digest.
    inherited = image("ghcr.io/nokia/srlinux@sha256:5678", digest=True, label=True)
    client = MagicMock()
    client.images.list.side_effect = lambda **kw: (
        [dangling, inherited]
        if kw
        else [*tagged, *untagged, stale, legacy, active, current]
    )
    client.containers.list.return_value = [SimpleNamespace(attrs={"Image": active.id})]
    monkeypatch.setattr(images, "_get_client", lambda: client)
    monkeypatch.setattr(images, "owned_images", lambda: ["nika/base:current"])
    monkeypatch.setattr(images, "_k8s_workload_images", lambda: vendor)

    actions = images.prune_stale_images()

    assert [call.args[0] for call in client.images.remove.call_args_list] == [
        "nika/base:old",
        "kathara/nika-base:latest",
        dangling.id,
    ]
    assert any("used by a container" in action for action in actions)
