"""Runtime settings of a template image: user, working directory, environment.

E2B builds a template from an image's filesystem, but its sandboxes start with
E2B's default user and environment instead of the image's. NIKA reads the
image config from the registry, sets the user and working directory in the
template, and passes the environment when it creates a sandbox. Configs are
cached per image digest, whose content never changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass

import httpx

from nika.config import REPO_ROOT

_CACHE_DIR = REPO_ROOT / ".nika_cache" / "image-configs"
_DOCKER_HUB = "registry-1.docker.io"
_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_TIMEOUT_SEC = 60


@dataclass(frozen=True)
class ImageConfig:
    user: str
    workdir: str
    env: dict[str, str]


def image_config(image: str) -> ImageConfig:
    """Return the runtime settings of *image*, a reference pinned by digest."""
    registry, repository, digest = _parse_reference(image)
    path = _CACHE_DIR / f"{digest.replace(':', '-')}.json"
    if path.is_file():
        return ImageConfig(**json.loads(path.read_text(encoding="utf-8")))
    config = _fetch_config(registry, repository, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    partial.replace(path)
    return config


def _parse_reference(image: str) -> tuple[str, str, str]:
    """Split *image* into registry host, repository and digest."""
    name, _, digest = image.partition("@")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Template image must be pinned by digest: {image!r}")
    host, _, rest = name.partition("/")
    if rest and ("." in host or ":" in host or host == "localhost"):
        registry, repository = host, rest
    else:
        registry, repository = _DOCKER_HUB, name
    if registry in ("docker.io", "index.docker.io"):
        registry = _DOCKER_HUB
    if registry == _DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    # A tag next to the digest only labels it.
    parent, _, last = repository.rpartition("/")
    last = last.split(":", 1)[0]
    return registry, f"{parent}/{last}" if parent else last, digest


def _fetch_config(registry: str, repository: str, digest: str) -> ImageConfig:
    """Read the config of every Linux platform of the image; they must agree."""
    base = f"https://{registry}/v2/{repository}"
    with _client() as client:
        top = json.loads(_get(client, f"{base}/manifests/{digest}", digest))
        if "manifests" in top:
            manifests = [
                json.loads(_get(client, f"{base}/manifests/{m['digest']}", m["digest"]))
                for m in top["manifests"]
                if (m.get("platform") or {}).get("os") == "linux"
            ]
        else:
            manifests = [top]
        configs = [
            _runtime_settings(
                json.loads(
                    _get(
                        client,
                        f"{base}/blobs/{m['config']['digest']}",
                        m["config"]["digest"],
                    )
                )
            )
            for m in manifests
        ]
    image = f"{registry}/{repository}@{digest}"
    if not configs:
        raise RuntimeError(f"{image} has no Linux image")
    if any(config != configs[0] for config in configs[1:]):
        raise RuntimeError(
            f"The platforms of {image} differ in user, working directory or "
            "environment, so one template cannot match them all"
        )
    return configs[0]


def _runtime_settings(blob: dict) -> ImageConfig:
    settings = blob.get("config") or {}
    env = dict(item.split("=", 1) for item in settings.get("Env") or [] if "=" in item)
    # Docker's defaults when the image sets none.
    return ImageConfig(
        user=settings.get("User") or "root",
        workdir=settings.get("WorkingDir") or "/",
        env=env,
    )


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=_TIMEOUT_SEC)


def _get(client: httpx.Client, url: str, digest: str) -> bytes:
    """GET content-addressed *url* and check it against *digest*."""
    response = client.get(url, headers={"Accept": _ACCEPT})
    if response.status_code == 401 and "Authorization" not in client.headers:
        token = _anonymous_token(client, response.headers.get("WWW-Authenticate", ""))
        client.headers["Authorization"] = f"Bearer {token}"
        response = client.get(url, headers={"Accept": _ACCEPT})
    response.raise_for_status()
    if f"sha256:{hashlib.sha256(response.content).hexdigest()}" != digest:
        raise RuntimeError(f"Registry content for {url} does not match {digest}")
    return response.content


def _anonymous_token(client: httpx.Client, challenge: str) -> str:
    """Pull token for a public repository (registry token authentication)."""
    scheme, _, params = challenge.partition(" ")
    fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
    if scheme.lower() != "bearer" or "realm" not in fields:
        raise RuntimeError(f"Unsupported registry authentication: {challenge!r}")
    realm = fields.pop("realm")
    response = client.get(realm, params=fields)
    response.raise_for_status()
    data = response.json()
    return data.get("token") or data["access_token"]
