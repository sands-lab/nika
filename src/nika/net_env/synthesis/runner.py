"""Execute the pinned NetComplete adapter for a declared routing sketch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4


def run_netcomplete(payload, directory: Path, *, image: str, timeout: float = 300):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.json").write_text(json.dumps(payload, indent=2) + "\n")
    # Never accept a stale solution when an invocation fails.
    (directory / "solution.json").unlink(missing_ok=True)
    container = f"nika-netcomplete-{uuid4().hex[:12]}"
    adapter = Path(__file__).with_name("netcomplete_adapter.py")
    try:
        with (directory / "solver.log").open("w") as log:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    container,
                    "--network",
                    "none",
                    "-v",
                    f"{directory}:/experiment",
                    "-v",
                    f"{adapter}:/adapter.py:ro",
                    image,
                    "python",
                    "/adapter.py",
                    "/experiment/input.json",
                    "/experiment/solution.json",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=timeout,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"NetComplete failed; see {directory / 'solver.log'}"
        ) from exc
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )
    return json.loads((directory / "solution.json").read_text())
