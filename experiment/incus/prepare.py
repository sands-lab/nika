"""Copy the selected runtime's host images and caches into an Incus base VM."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile

from nika.config import CACHE_DIR
from nika.firecracker.base import (
    CACHE_DIRS,
    _check_build_hash,
    _containerd_store,
    _runtime_images,
    _vendor_images,
)
from nika.net_env.utils.kathara.docker_files.docker_images import (
    NIKA_IMAGE_DOCKERFILES,
    dockerfile_parents,
)


def main() -> None:
    vm = sys.argv[1]

    def guest(*args: str) -> None:
        subprocess.run(["incus", "exec", vm, "--", *args], check=True)

    config = json.dumps({"features": {"containerd-snapshotter": _containerd_store()}})
    subprocess.run(
        ["incus", "exec", vm, "--", "tee", "/etc/docker/daemon.json"],
        input=config.encode(),
        check=True,
    )
    guest("systemctl", "restart", "docker")
    images = _runtime_images() + _vendor_images()
    parents = {
        parent
        for image in images
        if image in NIKA_IMAGE_DOCKERFILES
        for parent in dockerfile_parents(image)
        if "@sha256:" in parent
    }
    for image in dict.fromkeys(sorted(parents) + images):
        if "@sha256:" in image:
            guest("docker", "pull", "-q", image)
            continue
        if image in NIKA_IMAGE_DOCKERFILES:
            _check_build_hash(image)
        print(f"Copying host image {image}", flush=True)
        with tempfile.NamedTemporaryFile(suffix=".tar") as archive:
            subprocess.run(["docker", "save", "-o", archive.name, image], check=True)
            subprocess.run(
                ["incus", "file", "push", archive.name, f"{vm}/tmp/nika-image.tar"],
                check=True,
            )
            guest("docker", "load", "-q", "-i", "/tmp/nika-image.tar")
            guest("rm", "/tmp/nika-image.tar")
    guest("mkdir", "-p", "/home/ubuntu/nika/.nika_cache")
    for name in CACHE_DIRS:
        path = CACHE_DIR / name
        if path.is_dir():
            guest("mkdir", "-p", "/home/ubuntu/nika/.nika_cache")
            subprocess.run(
                [
                    "incus",
                    "file",
                    "push",
                    "-r",
                    str(path),
                    f"{vm}/home/ubuntu/nika/.nika_cache/",
                ],
                check=True,
            )
    guest("chown", "-R", "ubuntu:ubuntu", "/home/ubuntu/nika/.nika_cache")


if __name__ == "__main__":
    main()
