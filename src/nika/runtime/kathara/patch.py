"""Runtime fixes for the Kathara dependency."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from typing import Optional

_logger = logging.getLogger(__name__)


def patch_kathara_file_conversion() -> None:
    """Patch Kathara's text conversion helper to close file handles."""
    from Kathara import utils

    if getattr(utils.convert_win_2_linux, "_nika_closes_files", False):
        return

    def convert_win_2_linux(filename: str, write: bool = False) -> Optional[bytes]:
        if not utils.is_binary(filename):
            try:
                with open(filename, mode="r", encoding="utf-8-sig") as file_obj:
                    file_content = (
                        file_obj.read().replace("\n\r", "\n").replace("\r\n", "\n")
                    )
                if not write:
                    return file_content.encode("utf-8")
                with open(
                    filename, mode="w", encoding="utf-8", newline="\n"
                ) as file_obj_write:
                    file_obj_write.write(file_content)
                return None
            except Exception:
                pass

        if not write:
            with open(filename, mode="rb") as file_obj:
                return file_obj.read()
        return None

    convert_win_2_linux._nika_closes_files = True  # type: ignore[attr-defined]
    utils.convert_win_2_linux = convert_win_2_linux


_docker_reachable: bool | None = None
_docker_reachable_lock = threading.Lock()


def docker_engine_reachable() -> bool:
    """Ping the Docker engine once per process; later calls reuse the answer."""
    global _docker_reachable
    if _docker_reachable is None:
        with _docker_reachable_lock:
            if _docker_reachable is None:
                try:
                    from nika.runtime.shared.containers import docker_client

                    docker_client().ping()
                except Exception:
                    return False  # retry on the next call
                _docker_reachable = True
    return _docker_reachable


def allow_privileged_without_root(*, is_admin: bool | None = None) -> bool:
    """Return True when NIKA should bypass Kathara's host-root privileged gate."""
    if is_admin is None:
        from Kathara import utils

        is_admin = utils.is_admin()
    if is_admin:
        return False
    return docker_engine_reachable()


def patch_kathara_privileged_without_root() -> None:
    """Allow privileged Kathara devices without host root when Docker is usable.

    Kathara refuses ``privileged=true`` unless ``os.getuid() == 0``, even though
    members of the ``docker`` group can create privileged containers via the
    Docker API. Patching this gate lets k3s scenarios run under a normal user
    for batch benchmarks.
    """
    from Kathara import utils
    from Kathara.manager.docker.DockerMachine import DockerMachine

    if getattr(DockerMachine.create, "_nika_priv_without_root", False):
        return

    original_create = DockerMachine.create
    original_is_admin = utils.is_admin
    # Kathara also consults ``utils.is_admin`` to scope container listings to
    # the current user, so the override cannot be permanent. Parallel machine
    # creation shares one override: the first privileged create installs it,
    # the last one to finish restores the original.
    lock = threading.Lock()
    active = 0

    @contextlib.contextmanager
    def _admin_override() -> Iterator[None]:
        nonlocal active
        with lock:
            active += 1
            if active == 1:
                utils.is_admin = lambda: True
        try:
            yield
        finally:
            with lock:
                active -= 1
                if active == 0:
                    utils.is_admin = original_is_admin

    def create(self, machine):  # type: ignore[no-untyped-def]
        if machine.is_privileged() and allow_privileged_without_root(
            is_admin=original_is_admin()
        ):
            _logger.debug(
                "Allowing privileged Kathara device %r without host root "
                "(Docker engine reachable)",
                machine.name,
            )
            with _admin_override():
                return original_create(self, machine)
        return original_create(self, machine)

    create._nika_priv_without_root = True  # type: ignore[attr-defined]
    DockerMachine.create = create  # type: ignore[method-assign]


patch_kathara_file_conversion()
patch_kathara_privileged_without_root()
