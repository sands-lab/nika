"""The engine refuses lab images that 0.1.0 did not deploy, without retagging."""

from __future__ import annotations

import pytest

from nika.benchmark_engine import images
from nika.benchmark_engine.protocol import EngineError


class _Image:
    def __init__(self, layers, *, digests=(), created_by=""):
        self.attrs = {"RootFS": {"Layers": list(layers)}, "RepoDigests": list(digests)}
        self._created_by = created_by
        self.tagged: list[tuple] = []

    def history(self):
        return [{"CreatedBy": self._created_by}]

    def tag(self, repo, tag):
        self.tagged.append((repo, tag))


class _Client:
    def __init__(self, found):
        self.found = found
        self.images = self

    def get(self, name):
        from docker.errors import ImageNotFound

        if name not in self.found:
            raise ImageNotFound(name)
        return self.found[name]

    def pull(self, repo, tag):
        raise AssertionError(f"unexpected pull of {repo}@{tag}")


def _host(*, nika_run: str | None = None, nika_parent: str = "kathara/base"):
    found = {}
    for repo, digest in images.UPSTREAM_IMAGES.items():
        image = _Image([f"{repo}-layer"], digests=[f"{repo}@{digest}"])
        found[f"{repo}:latest"] = found[f"{repo}@{digest}"] = image
    parent, digest, run = images._dockerfile("kathara/nika-base")
    found[f"{parent}@{digest}"] = found[f"{parent}:latest"]
    created_by = "/bin/sh -c " + (run if nika_run is None else nika_run)
    found["kathara/nika-base"] = _Image(
        [f"{nika_parent}-layer", "run-layer"], created_by=created_by
    )
    return _Client(found)


def _check(monkeypatch, client) -> None:
    import docker

    monkeypatch.setattr(docker, "from_env", lambda: client)
    images.ensure_lab_images()


def test_images_built_from_the_release_dockerfile_pass(monkeypatch) -> None:
    _check(monkeypatch, _host())


def test_an_image_built_from_another_dockerfile_is_refused(monkeypatch) -> None:
    with pytest.raises(EngineError, match="kathara/nika-base"):
        _check(monkeypatch, _host(nika_run="apt-get update && apt-get install -y x"))


def test_an_image_built_on_another_base_is_refused(monkeypatch) -> None:
    with pytest.raises(EngineError, match="kathara/nika-base"):
        _check(monkeypatch, _host(nika_parent="other"))


def test_a_different_upstream_latest_is_refused_not_retagged(monkeypatch) -> None:
    client = _host()
    client.found["kathara/p4:latest"] = _Image(["x"], digests=["kathara/p4@sha256:0"])
    with pytest.raises(EngineError, match="kathara/p4:latest"):
        _check(monkeypatch, client)


def test_dockerfile_run_matches_classic_and_buildkit_history() -> None:
    _, _, run = images._dockerfile("kathara/nika-frr")
    assert images._normalize("/bin/sh -c " + run) == run
    assert images._normalize("RUN /bin/sh -c " + run + " # buildkit") == run
