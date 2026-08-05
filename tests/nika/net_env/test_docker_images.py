"""Unit tests for Docker image ensure (build vs pull)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nika.net_env.kathara.utils.docker_files import docker_images as di


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    di._client = None
    yield
    di._client = None


def test_ensure_builds_local_nika_images_and_pulls_upstream() -> None:
    existing: set[str] = set()

    def fake_exists(image: str) -> bool:
        return image in existing

    def fake_build(image: str) -> None:
        existing.add(image)

    def fake_pull(image: str) -> None:
        existing.add(image)

    with (
        patch.object(di, "image_exists", side_effect=fake_exists),
        patch.object(di, "build_nika_image", side_effect=fake_build) as build,
        patch.object(di, "pull_image", side_effect=fake_pull) as pull,
    ):
        di.ensure_nika_docker_images(["kathara/nika-base", "kathara/p4", "kathara/sdn"])

    build.assert_called_once_with("kathara/nika-base")
    assert sorted(c.args[0] for c in pull.call_args_list) == [
        "kathara/p4",
        "kathara/sdn",
    ]


def test_ensure_skips_when_all_present() -> None:
    with (
        patch.object(di, "image_exists", return_value=True),
        patch.object(di, "build_nika_image") as build,
        patch.object(di, "pull_image") as pull,
    ):
        di.ensure_nika_docker_images(["kathara/nika-base", "kathara/p4"])

    build.assert_not_called()
    pull.assert_not_called()


def test_ensure_raises_if_still_missing() -> None:
    with (
        patch.object(di, "image_exists", return_value=False),
        patch.object(di, "build_nika_image"),
        patch.object(di, "pull_image"),
    ):
        with pytest.raises(RuntimeError, match="Failed to ensure"):
            di.ensure_nika_docker_images(["kathara/nika-base"])
