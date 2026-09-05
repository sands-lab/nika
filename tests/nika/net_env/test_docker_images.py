"""Unit tests for Docker image ensure (build vs pull) and platform pinning."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nika.net_env.utils.kathara.docker_files import docker_images as di


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
        di.ensure_nika_docker_images(
            ["nika/base", "nika/onos", "kathara/p4", "kathara/sdn"]
        )

    assert sorted(c.args[0] for c in build.call_args_list) == [
        "nika/base",
        "nika/onos",
    ]
    assert sorted(c.args[0] for c in pull.call_args_list) == [
        "kathara/p4",
        "kathara/sdn",
    ]


def test_nika_fabric_controller_dockerfile_is_registered() -> None:
    assert "nika/fabric-controller" in di.NIKA_IMAGE_DOCKERFILES
    assert di._is_locally_buildable("nika/fabric-controller")
    assert di._dockerfile_for_image("nika/fabric-controller").is_file()


def test_nika_onos_builds_for_host_architecture() -> None:
    assert "nika/onos" not in di.NIKA_IMAGE_PLATFORMS
    assert "nika/onos" in di.NIKA_IMAGE_BUILDKIT
    text = di._dockerfile_for_image("nika/onos").read_text(encoding="utf-8")
    assert "FROM kathara/base:latest" in text
    assert "eclipse-temurin:11-jre-jammy" in text
    assert "COPY --from=onos-dist /root/onos /root/onos" in text
    assert "jdk.util.zip.disableZip64ExtraFieldValidation" in text


def test_build_nika_image_uses_docker_cli_for_onos() -> None:
    with (
        patch.object(di.subprocess, "run") as run_cli,
        patch.object(di, "_get_client") as get_client,
    ):
        di.build_nika_image("nika/onos")

    run_cli.assert_called_once()
    cmd = run_cli.call_args.args[0]
    assert cmd[0:2] == ["docker", "build"]
    assert "-f" in cmd and "Dockerfile.onos" in cmd
    assert "nika/onos" in cmd
    assert run_cli.call_args.kwargs["env"]["DOCKER_BUILDKIT"] == "1"
    get_client.assert_not_called()


def test_build_nika_image_omits_platform_for_multiarch_base() -> None:
    fake_client = MagicMock()
    fake_client.images.build.return_value = (MagicMock(), iter([]))

    with patch.object(di, "_get_client", return_value=fake_client):
        di.build_nika_image("nika/base")

    assert "platform" not in fake_client.images.build.call_args.kwargs


def test_build_nika_image_passes_platform_when_configured() -> None:
    fake_client = MagicMock()
    fake_client.images.build.return_value = (MagicMock(), iter([]))
    fake_client.images.get.return_value = SimpleNamespace(
        attrs={"Architecture": "amd64"}
    )

    with (
        patch.object(di, "NIKA_IMAGE_PLATFORMS", {"nika/base": "linux/amd64"}),
        patch.object(di, "_get_client", return_value=fake_client),
        patch.object(di, "host_can_run_amd64", return_value=True),
    ):
        di.build_nika_image("nika/base")

    assert fake_client.images.build.call_args.kwargs["platform"] == "linux/amd64"


def test_ensure_skips_when_all_present() -> None:
    with (
        patch.object(di, "image_exists", return_value=True),
        patch.object(di, "build_nika_image") as build,
        patch.object(di, "pull_image") as pull,
    ):
        di.ensure_nika_docker_images(["nika/base", "kathara/p4"])

    build.assert_not_called()
    pull.assert_not_called()


def test_ensure_retags_legacy_image_instead_of_building() -> None:
    existing = {"kathara/nika-base"}

    def fake_exists(image: str) -> bool:
        return image in existing

    def fake_retag(source: str, target: str) -> None:
        existing.discard(source)
        existing.add(target)

    with (
        patch.object(di, "image_exists", side_effect=fake_exists),
        patch.object(di, "retag_image", side_effect=fake_retag) as retag,
        patch.object(di, "build_nika_image") as build,
        patch.object(di, "pull_image") as pull,
    ):
        di.ensure_nika_docker_images(["nika/base"])

    retag.assert_called_once_with("kathara/nika-base", "nika/base")
    build.assert_not_called()
    pull.assert_not_called()


def test_ensure_raises_if_still_missing() -> None:
    with (
        patch.object(di, "image_exists", return_value=False),
        patch.object(di, "build_nika_image"),
        patch.object(di, "pull_image"),
    ):
        with pytest.raises(RuntimeError, match="Failed to ensure"):
            di.ensure_nika_docker_images(["nika/base"])
