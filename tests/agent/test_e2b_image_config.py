"""Template image settings read from the registry for e2b sandboxes."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from nika.agents.sandbox.e2b import image_config as module
from nika.agents.sandbox.e2b.image_config import ImageConfig, image_config

pytestmark = pytest.mark.unit

_DIGEST = "sha256:" + "a" * 64


def test_parse_reference_normalizes_registry_and_drops_tag() -> None:
    parse = module._parse_reference
    assert parse(f"docker/sandbox-templates:claude@{_DIGEST}") == (
        "registry-1.docker.io",
        "docker/sandbox-templates",
        _DIGEST,
    )
    assert parse(f"ubuntu:24.04@{_DIGEST}") == (
        "registry-1.docker.io",
        "library/ubuntu",
        _DIGEST,
    )
    assert parse(f"localhost:5000/team/img:v1@{_DIGEST}") == (
        "localhost:5000",
        "team/img",
        _DIGEST,
    )
    with pytest.raises(ValueError, match="pinned by digest"):
        parse("ubuntu:24.04")


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _registry(
    monkeypatch, tmp_path, platform_envs: list[list[str]]
) -> tuple[str, list]:
    """Serve one multi-platform image behind an anonymous token challenge."""
    blobs: dict[str, bytes] = {}

    def add(document: dict) -> str:
        data = json.dumps(document).encode()
        blobs[_digest(data)] = data
        return _digest(data)

    platforms = []
    for arch, env in zip(("amd64", "arm64"), platform_envs, strict=False):
        config = add({"config": {"User": "agent", "WorkingDir": "/w", "Env": env}})
        manifest = add({"config": {"digest": config}, "layers": []})
        platforms.append(
            {"digest": manifest, "platform": {"os": "linux", "architecture": arch}}
        )
    attestation = add({"config": {"digest": add({})}, "layers": []})
    platforms.append({"digest": attestation, "platform": {"os": "unknown"}})
    index = add({"manifests": platforms})
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"token": "anon"})
        if request.headers.get("Authorization") != "Bearer anon":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.example/token",'
                    'service="registry.example",scope="repository:team/img:pull"'
                },
            )
        return httpx.Response(200, content=blobs[request.url.path.rsplit("/", 1)[1]])

    monkeypatch.setattr(
        module,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(module, "_CACHE_DIR", tmp_path / "cache")
    return f"registry.example/team/img:v1@{index}", requests


def test_image_config_reads_linux_platforms_then_uses_cache(
    monkeypatch, tmp_path
) -> None:
    env = ["PATH=/home/agent/.local/bin:/usr/bin", "EMPTY="]
    image, requests = _registry(monkeypatch, tmp_path, [env, env])

    expected = ImageConfig(
        user="agent",
        workdir="/w",
        env={"PATH": "/home/agent/.local/bin:/usr/bin", "EMPTY": ""},
    )
    assert image_config(image) == expected
    fetched = len(requests)
    assert image_config(image) == expected
    assert len(requests) == fetched


def test_image_config_rejects_platforms_that_differ(monkeypatch, tmp_path) -> None:
    image, _ = _registry(monkeypatch, tmp_path, [["PATH=/a"], ["PATH=/b"]])

    with pytest.raises(RuntimeError, match="platforms .* differ"):
        image_config(image)
