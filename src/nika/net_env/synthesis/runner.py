"""Execute the pinned NetComplete adapter for a declared routing sketch."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import docker

from nika.utils.docker_files import read_file, upload_paths


def run_netcomplete(payload, directory: Path, *, image: str, timeout: float = 300):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.json").write_text(json.dumps(payload, indent=2) + "\n")
    (directory / "solution.json").unlink(missing_ok=True)
    client = docker.from_env()
    try:
        container = client.containers.create(
            image,
            command=[
                "python",
                "/adapter.py",
                "/experiment/input.json",
                "/experiment/solution.json",
            ],
            name=f"nika-netcomplete-{uuid4().hex[:12]}",
            network_disabled=True,
        )
        try:
            upload_paths(
                container,
                {
                    "/adapter.py": Path(__file__).with_name("netcomplete_adapter.py"),
                    "/experiment": directory,
                },
            )
            container.start()
            try:
                status = container.wait(timeout=timeout)["StatusCode"]
            finally:
                (directory / "solver.log").write_bytes(container.logs())
            if status:
                raise RuntimeError(
                    f"NetComplete failed; see {directory / 'solver.log'}"
                )
            (directory / "solution.json").write_bytes(
                read_file(container, "/experiment/solution.json")
            )
        finally:
            container.remove(force=True)
    finally:
        client.close()
    return json.loads((directory / "solution.json").read_text())
