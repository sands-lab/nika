"""Build and run benchmark engine containers (contract B).

A release that pins a benchmark engine runs on a container built from its
release tag. The container shares the host's Docker daemon, network
namespaces, and NIKA state with the frontend, so environments, sessions, and
trial directories look the same as when the current NIKA's engine runs them in
process.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika.benchmark_engine.protocol import ENGINE_TOKEN_HEADER
from nika.config import CACHE_DIR, ENV_STATE_DIR, REPO_ROOT, RUNTIME_DIR, STATE_DIR
from nika.config import clab_runtime_root

ENGINE_IMAGE_REPO = "nika/benchmark-engine"
COMMIT_LABEL = "org.sands-lab.nika.commit"
RUN_LABEL = "org.sands-lab.nika.engine-run"
DOCKERFILE = "src/nika/benchmark_engine/docker/Dockerfile"
ENV_ENGINE_TOKEN = "NIKA_ENGINE_TOKEN"
_READY_TIMEOUT_SEC = 120.0


class EngineContainerError(RuntimeError):
    """A benchmark engine image could not be built or its container started."""


def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def _git_commit(ref: str) -> str:
    result = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=REPO_ROOT)
    if result.returncode != 0:
        raise EngineContainerError(
            f"Git ref {ref!r} not found in {REPO_ROOT}; fetch the release tags "
            "(git fetch --tags) and retry."
        )
    return result.stdout.strip()


def _commit_date(commit: str) -> str:
    result = _run(["git", "show", "-s", "--format=%cI", commit], cwd=REPO_ROOT)
    return result.stdout.strip()


def _image_label(image: str, label: str) -> str | None:
    result = _run(["docker", "image", "inspect", image, "--format", "{{json .}}"])
    if result.returncode != 0:
        return None
    labels = (json.loads(result.stdout).get("Config") or {}).get("Labels") or {}
    return labels.get(label)


def image_id(image: str) -> str | None:
    result = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def build_engine_image(ref: str) -> str:
    """Build the benchmark engine image of Git *ref*; return its local name.

    The build context is ``git archive`` of the commit, so uncommitted changes
    never reach an engine image.
    """
    commit = _git_commit(ref)
    image = f"{ENGINE_IMAGE_REPO}:{re.sub(r'[^A-Za-z0-9_.-]', '-', ref)[:128]}"
    with tempfile.TemporaryDirectory(prefix="nika-engine-") as tmp:
        archive = subprocess.Popen(
            ["git", "archive", "--format=tar", commit],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
        )
        extract = _run(["tar", "-x", "-C", tmp], stdin=archive.stdout)
        archive.wait()
        if archive.returncode != 0 or extract.returncode != 0:
            raise EngineContainerError(f"Could not export {ref} ({commit}).")
        if not (Path(tmp) / DOCKERFILE).is_file():
            raise EngineContainerError(
                f"{ref} ({commit[:12]}) has no benchmark engine ({DOCKERFILE})."
            )
        build = subprocess.run(
            [
                "docker",
                "build",
                "-f",
                str(Path(tmp) / DOCKERFILE),
                "--build-arg",
                f"NIKA_COMMIT={commit}",
                "--build-arg",
                f"NIKA_COMMIT_DATE={_commit_date(commit)}",
                "--label",
                f"{COMMIT_LABEL}={commit}",
                "-t",
                image,
                tmp,
            ],
            check=False,
        )
    if build.returncode != 0:
        raise EngineContainerError(f"Building the benchmark engine of {ref} failed.")
    return image


def resolve_engine_image(engine: dict[str, Any]) -> str:
    """Return a local image for a manifest's ``engine`` block.

    Uses ``engine.image`` when it is present locally or can be pulled, and
    otherwise builds the image from ``engine.tag``.
    """
    image = engine.get("image")
    if image and (image_id(image) or _run(["docker", "pull", image]).returncode == 0):
        return str(image)
    tag = engine.get("tag")
    if not tag:
        raise EngineContainerError(
            f"Benchmark engine image {image!r} is unavailable and the manifest "
            "names no tag to build it from."
        )
    local = f"{ENGINE_IMAGE_REPO}:{tag}"
    if _image_label(local, COMMIT_LABEL) == _git_commit(str(tag)):
        return local
    return build_engine_image(str(tag))


@dataclass
class EngineContainer:
    name: str
    image: str
    image_id: str | None
    base_url: str
    token: str


def _free_port() -> int:
    from nika.utils.net import pick_free_port

    return pick_free_port("127.0.0.1")


def _bind(source: Path | str, target: Path | str | None = None, *, ro=False) -> list:
    spec = f"type=bind,src={source},dst={target or source}"
    return ["--mount", spec + (",readonly" if ro else "")]


def _docker_run_args(
    image: str, *, name: str, run_id: str, results_root: Path, port: int, token: str
) -> list[str]:
    for path in (RUNTIME_DIR, CACHE_DIR, results_root):
        path.mkdir(parents=True, exist_ok=True)
    clab_dir = clab_runtime_root(create=True)
    args = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--label",
        f"{RUN_LABEL}={run_id}",
        # Containerlab and Kathara need the host's network and process
        # namespaces and full privileges, as on a host install.
        "--network",
        "host",
        "--pid",
        "host",
        "--privileged",
        "-e",
        f"{ENV_STATE_DIR}={STATE_DIR}",
        "-e",
        f"NIKA_UID={os.getuid()}",
        "-e",
        f"NIKA_GID={os.getgid()}",
        "-e",
        f"{ENV_ENGINE_TOKEN}={token}",
        *_bind("/var/run/docker.sock"),
        *_bind(RUNTIME_DIR),
        *_bind(CACHE_DIR),
        *_bind(results_root),
        *_bind(clab_dir),
        *_bind("/lib/modules", ro=True),
    ]
    if Path("/var/run/netns").is_dir():
        args += [
            "--mount",
            "type=bind,src=/var/run/netns,dst=/var/run/netns,bind-propagation=rshared",
        ]
    kathara_conf = Path.home() / ".config" / "kathara.conf"
    if kathara_conf.is_file():
        args += _bind(kathara_conf, "/run/nika/kathara.conf", ro=True)
    # The image's entrypoint starts the engine service with these flags.
    return args + [
        image,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--parent-pid",
        str(os.getpid()),
    ]


def _wait_ready(container: EngineContainer) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_SEC
    request = urllib.request.Request(
        f"{container.base_url}/health", headers={ENGINE_TOKEN_HEADER: container.token}
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=2.0):
                return
        except (urllib.error.URLError, OSError):
            pass
        state = _run(["docker", "inspect", "-f", "{{.State.Running}}", container.name])
        if state.stdout.strip() != "true":
            break
        time.sleep(0.5)
    logs = _run(["docker", "logs", "--tail", "40", container.name])
    raise EngineContainerError(
        f"Benchmark engine container {container.name} did not become ready.\n"
        + (logs.stdout + logs.stderr).strip()
    )


@contextmanager
def engine_container(
    image: str, *, run_id: str, results_root: Path
) -> Iterator[EngineContainer]:
    """Run one benchmark engine container for a run, then stop it.

    The container's log is appended to ``benchmark_engine.log`` in
    *results_root*, after the logs of earlier containers for the same results.
    The engine also exits by itself when this process ends.
    """
    port = _free_port()
    token = secrets.token_urlsafe(32)
    name = f"nika-engine-{run_id}-{secrets.token_hex(3)}"
    args = _docker_run_args(
        image,
        name=name,
        run_id=run_id,
        results_root=results_root,
        port=port,
        token=token,
    )
    started = _run(args)
    if started.returncode != 0:
        raise EngineContainerError(
            f"Could not start the benchmark engine container: {started.stderr.strip()}"
        )
    container = EngineContainer(
        name=name,
        image=image,
        image_id=image_id(image),
        base_url=f"http://127.0.0.1:{port}",
        token=token,
    )
    try:
        _wait_ready(container)
        yield container
    finally:
        logs = _run(["docker", "logs", name])
        with (results_root / "benchmark_engine.log").open("a", encoding="utf-8") as log:
            log.write(logs.stdout + logs.stderr)
        _run(["docker", "stop", "--time", "30", name])
