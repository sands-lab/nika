"""Unit tests for Docker image ensure (build vs pull) and platform pinning."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nika.net_env.utils.kathara.docker_files import docker_images as di

pytestmark = pytest.mark.unit

BASE = di.nika_image("base")
ONOS = di.nika_image("onos")
FABRIC = di.nika_image("fabric-controller")
_tag_pinned_image = di._tag_pinned_image


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    di._client = None
    with (
        patch.object(di, "_local_image_id", side_effect=lambda ref: f"id-{ref}"),
        patch.object(di, "_get_client", side_effect=AssertionError("no Docker")),
        patch.object(di, "_previous_builds", return_value=[]),
        patch.object(di, "_tag_pinned_image"),
    ):
        yield
    di._client = None


def test_ensure_builds_local_nika_images_and_pulls_upstream() -> None:
    existing: set[str] = set()

    def fake_exists(image: str) -> bool:
        return image in existing

    def fake_build(image: str, expected_hash: str | None = None) -> None:
        existing.add(image)

    def fake_pull(image: str) -> None:
        existing.add(image)

    with (
        patch.object(di, "image_exists", side_effect=fake_exists),
        patch.object(di, "build_nika_image", side_effect=fake_build) as build,
        patch.object(di, "pull_image", side_effect=fake_pull) as pull,
    ):
        di.ensure_nika_docker_images(
            [BASE, ONOS, di.KATHARA_P4_IMAGE, di.KATHARA_SDN_IMAGE]
        )

    assert sorted(c.args[0] for c in build.call_args_list) == [BASE, ONOS]
    # Digest-pinned build parents are pulled by docker build itself.
    assert sorted(c.args[0] for c in pull.call_args_list) == [
        di.KATHARA_P4_IMAGE,
        di.KATHARA_SDN_IMAGE,
    ]


def test_ensure_builds_local_parent_before_child() -> None:
    existing: set[str] = set()
    order: list[str] = []

    def fake_build(image: str, expected_hash: str | None = None) -> None:
        order.append(image)
        existing.add(image)

    with (
        patch.object(di, "image_exists", side_effect=lambda i: i in existing),
        patch.object(di, "build_nika_image", side_effect=fake_build),
        patch.object(di, "pull_image", side_effect=existing.add),
    ):
        di.ensure_nika_docker_images([FABRIC])

    assert order == [BASE, FABRIC]


def test_ensure_rebuilds_outdated_image() -> None:
    with (
        patch.object(di, "image_exists", return_value=True),
        patch.object(di, "_image_build_hash", return_value="stale"),
        patch.object(di, "build_nika_image") as build,
        patch.object(di, "pull_image") as pull,
    ):
        di.ensure_nika_docker_images([BASE])

    build.assert_called_once_with(BASE, expected_hash=di.build_hash(BASE))
    pull.assert_not_called()


def test_build_hash_tracks_local_parent_image_id() -> None:
    before = di.build_hash(FABRIC)
    with patch.object(di, "_local_image_id", return_value="newer-nika-base"):
        assert di.build_hash(FABRIC) != before


def test_build_hash_ignores_nika_tag() -> None:
    # A retag keeps the parent image ID; only the tag differs.
    with patch.object(di, "_local_image_id", return_value="same-nika-base"):
        before = di.build_hash(FABRIC)
    with (
        patch.dict(di._BUILD_ARGS, {di.NIKA_IMAGE_TAG_ARG: "9.9.9"}),
        patch.object(di, "_local_image_id", return_value="same-nika-base"),
    ):
        assert di.dockerfile_parents(FABRIC) == ["nika/base:9.9.9"]
        assert di.build_hash(FABRIC) == before


def test_dockerfile_parents_resolve_args_and_skip_stages() -> None:
    parents = di.dockerfile_parents(ONOS)
    assert parents[0].startswith("onosproject/onos:2.7-latest@sha256:")
    assert parents[1].startswith("eclipse-temurin:11-jre-jammy@sha256:")
    assert parents[2] == di.KATHARA_BASE_IMAGE


def test_nika_fabric_controller_dockerfile_is_registered() -> None:
    assert FABRIC in di.NIKA_IMAGE_DOCKERFILES
    assert di._is_locally_buildable(FABRIC)
    assert di._dockerfile_for_image(FABRIC).is_file()


def test_nika_onos_builds_for_host_architecture() -> None:
    assert ONOS not in di.NIKA_IMAGE_PLATFORMS
    assert ONOS in di.NIKA_IMAGE_BUILDKIT
    text = di._dockerfile_for_image(ONOS).read_text(encoding="utf-8")
    assert f"FROM {di.KATHARA_BASE_IMAGE}" in text
    assert "eclipse-temurin:11-jre-jammy" in text
    assert "COPY --from=onos-dist /root/onos /root/onos" in text
    assert "jdk.util.zip.disableZip64ExtraFieldValidation" in text


def test_build_nika_image_uses_docker_cli_for_onos() -> None:
    with (
        patch.object(di.subprocess, "run") as run_cli,
        patch.object(di, "_get_client") as get_client,
    ):
        di.build_nika_image(ONOS)

    run_cli.assert_called_once()
    cmd = run_cli.call_args.args[0]
    assert cmd[0:2] == ["docker", "build"]
    assert "-f" in cmd and "Dockerfile.onos" in cmd
    assert ONOS in cmd
    assert f"--build-arg=NIKA_IMAGE_TAG={di.NIKA_IMAGE_TAG}" in cmd
    assert f"--label={di.IMAGE_LABEL}={ONOS}" in cmd
    assert run_cli.call_args.kwargs["env"]["DOCKER_BUILDKIT"] == "1"
    get_client.assert_not_called()


def test_build_nika_image_omits_platform_for_multiarch_base() -> None:
    fake_client = MagicMock()
    fake_client.images.build.return_value = (MagicMock(), iter([]))

    with patch.object(di, "_get_client", return_value=fake_client):
        di.build_nika_image(BASE)

    kwargs = fake_client.images.build.call_args.kwargs
    assert "platform" not in kwargs
    assert kwargs["buildargs"] == {"NIKA_IMAGE_TAG": di.NIKA_IMAGE_TAG}
    assert kwargs["labels"] == {
        di.IMAGE_LABEL: BASE,
        di.BUILD_HASH_LABEL: di.build_hash(BASE),
    }


def test_build_nika_image_passes_platform_when_configured() -> None:
    fake_client = MagicMock()
    fake_client.images.build.return_value = (MagicMock(), iter([]))
    fake_client.images.get.return_value = SimpleNamespace(
        attrs={"Architecture": "amd64"}
    )

    with (
        patch.object(di, "NIKA_IMAGE_PLATFORMS", {BASE: "linux/amd64"}),
        patch.object(di, "_get_client", return_value=fake_client),
        patch.object(di, "host_can_run_amd64", return_value=True),
    ):
        di.build_nika_image(BASE)

    assert fake_client.images.build.call_args.kwargs["platform"] == "linux/amd64"


def test_ensure_skips_when_all_present_and_current() -> None:
    with (
        patch.object(di, "image_exists", return_value=True),
        patch.object(di, "_image_build_hash", side_effect=di.build_hash),
        patch.object(di, "build_nika_image") as build,
        patch.object(di, "pull_image") as pull,
    ):
        di.ensure_nika_docker_images([BASE, di.KATHARA_P4_IMAGE])

    build.assert_not_called()
    pull.assert_not_called()


@pytest.mark.parametrize("label", ["current", "unversioned"])
def test_ensure_retags_identical_previous_build(label: str) -> None:
    existing = {"nika/base:latest"}
    labels = {
        "current": di.build_hash(BASE),
        "unversioned": di._unversioned_build_hash(BASE),
    }

    def fake_retag(source: str, target: str) -> None:
        existing.discard(source)
        existing.add(target)

    with (
        patch.object(di, "_previous_builds", return_value=["nika/base:latest"]),
        patch.object(di, "image_exists", side_effect=lambda i: i in existing),
        patch.object(di, "_image_build_hash", return_value=labels[label]),
        patch.object(di, "retag_image", side_effect=fake_retag) as retag,
        patch.object(di, "build_nika_image") as build,
    ):
        di.ensure_nika_docker_images([BASE])

    retag.assert_called_once_with("nika/base:latest", BASE)
    build.assert_not_called()


def test_ensure_removes_outdated_previous_build_then_rebuilds() -> None:
    fake_client = MagicMock()
    with (
        patch.object(di, "_previous_builds", return_value=["kathara/nika-base"]),
        patch.object(di, "_get_client", return_value=fake_client),
        patch.object(di, "image_exists", return_value=False),
        patch.object(di, "_image_build_hash", return_value="stale"),
        patch.object(di, "retag_image") as retag,
        patch.object(di, "build_nika_image") as build,
    ):
        with pytest.raises(RuntimeError, match="Failed to ensure"):
            di.ensure_nika_docker_images([BASE])

    retag.assert_not_called()
    fake_client.images.remove.assert_called_once_with("kathara/nika-base")
    build.assert_called_once_with(BASE, expected_hash=di.build_hash(BASE))


def test_ensure_raises_if_still_missing() -> None:
    with (
        patch.object(di, "image_exists", return_value=False),
        patch.object(di, "build_nika_image"),
        patch.object(di, "pull_image"),
    ):
        with pytest.raises(RuntimeError, match="Failed to ensure"):
            di.ensure_nika_docker_images([BASE])


def test_every_deployed_image_is_pinned() -> None:
    """Benchmark reproducibility: no floating upstream tag, nika/* on the release tag."""
    from agent.sandbox.sbx.agents import NATIVE_SBX_TEMPLATE_IMAGES
    from nika.workflows.setup.images import build_parent_images, runtime_images

    images = {
        *runtime_images(),
        *build_parent_images(),
        *NATIVE_SBX_TEMPLATE_IMAGES.values(),
    }
    for image in sorted(images):
        if image.startswith(di.NIKA_IMAGE_PREFIX):
            assert image == f"{di.image_repository(image)}:{di.NIKA_IMAGE_TAG}"
        else:
            assert "@sha256:" in image, f"{image} is not pinned by digest"


def test_static_lab_confs_use_deployed_images() -> None:
    """Kathara lab.conf files cannot be templated; keep them on the deployed refs."""
    import re

    from nika.config import REPO_ROOT
    from nika.workflows.setup.images import runtime_images

    deployed = set(runtime_images())
    for conf in sorted((REPO_ROOT / "src/nika/net_env").rglob("lab.conf")):
        text = conf.read_text(encoding="utf-8")
        for image in re.findall(r'\[image\]="([^"]+)"', text):
            assert image in deployed, f"{conf.relative_to(REPO_ROOT)}: {image}"


def test_tag_pinned_image_names_the_pinned_version() -> None:
    """Kathara reads a machine's image tag; a digest pull leaves none."""
    fake_client = MagicMock()
    ids = {di.KATHARA_P4_IMAGE: "pinned", "kathara/p4:latest": None}
    with (
        patch.object(di, "_get_client", return_value=fake_client),
        patch.object(di, "_local_image_id", side_effect=ids.get),
    ):
        _tag_pinned_image(di.KATHARA_P4_IMAGE)
        ids["kathara/p4:latest"] = "pinned"
        _tag_pinned_image(di.KATHARA_P4_IMAGE)

    fake_client.images.get.assert_called_once_with(di.KATHARA_P4_IMAGE)
    fake_client.images.get.return_value.tag.assert_called_once_with(
        "kathara/p4", tag="latest"
    )
