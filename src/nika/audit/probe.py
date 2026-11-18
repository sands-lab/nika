"""Replay a process against a pinned artifact and one explicit input change."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nika.audit.source import read_source, source_files


class ProbePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    commit: str
    source_path: str
    mount_path: str
    commands: list[list[str]] = Field(min_length=1, max_length=3)
    append: str = Field(min_length=1, max_length=32)

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[a-z0-9][a-z0-9._:/-]*@)?sha256:[0-9a-f]{64}", value):
            raise ValueError("probe image must be pinned by SHA256")
        return value

    @field_validator("source_path")
    @classmethod
    def source_path_inside_code(cls, value: str) -> str:
        if (
            not value.startswith(("src/nika/", "src/agent/"))
            or ".." in Path(value).parts
        ):
            raise ValueError("probe source must stay under src/nika/ or src/agent/")
        return value

    @field_validator("mount_path")
    @classmethod
    def artifact_path(cls, value: str) -> str:
        path = Path(value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(c in value for c in ",:\x00\n")
        ):
            raise ValueError("probe mount must be an absolute container file path")
        if len(path.parts) < 3 or path.parts[1] in {"proc", "sys", "dev", "run"}:
            raise ValueError("probe mount must name an artifact file")
        return value

    @field_validator("commands")
    @classmethod
    def bounded_argv(cls, value: list[list[str]]) -> list[list[str]]:
        if any(
            not argv or len(argv) > 32 or any("\x00" in arg for arg in argv)
            for argv in value
        ):
            raise ValueError("probe commands need bounded argv lists without NUL bytes")
        return value


def _command(image: str, target: str, data: bytes, argv: list[str]) -> dict:
    name = "nika-audit-probe-" + uuid.uuid4().hex
    start = time.monotonic()
    result = {"argv": argv, "container": name}
    with tempfile.TemporaryDirectory(prefix="nika-audit-probe-") as directory:
        artifact = Path(directory) / "input"
        artifact.write_bytes(data)
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_RAW",
            "--mount",
            f"type=bind,src={artifact},dst={target},readonly",
            "--entrypoint",
            argv[0],
            image,
            *argv[1:],
        ]
        # Spool process output so a noisy command does not fill agent memory.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                completed = subprocess.run(
                    command, stdout=stdout, stderr=stderr, timeout=3
                )
                result.update(returncode=completed.returncode, timed_out=False)
            except subprocess.TimeoutExpired:
                result.update(returncode=None, timed_out=True)
            finally:
                # The UUID belongs to this call; never remove another container.
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", name], capture_output=True, timeout=10
                    )
                    remaining = subprocess.run(
                        ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=True,
                    )
                    result["resources_removed"] = not remaining.stdout.strip()
                except (OSError, subprocess.SubprocessError) as exc:
                    result.update(resources_removed=False, cleanup_error=str(exc))
            for label, handle in (("stdout", stdout), ("stderr", stderr)):
                size = handle.tell()
                handle.seek(0)
                result[label] = handle.read(64_000).decode(errors="replace")
                result[label + "_truncated"] = size > 64_000
    result["seconds"] = round(time.monotonic() - start, 3)
    return result


def run_probe(plan: ProbePlan, repo: Path, workdir: Path) -> dict:
    """Measure both conditions; interpretation belongs to a separate reviewer."""
    source_files(str(repo.resolve()), plan.commit)  # Validate the immutable commit.
    original = read_source(str(repo.resolve()), plan.commit, plan.source_path).encode()
    if len(original) > 64_000:
        raise ValueError("probe source artifact exceeds 64000 bytes")
    image = subprocess.run(
        ["docker", "image", "inspect", plan.image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    request = {
        "plan": plan.model_dump(),
        "image_id": image,
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "isolation": {"network": "none", "read_only": True, "timeout_seconds": 3},
    }
    key = hashlib.sha256(
        json.dumps([request, Path(__file__).read_text()]).encode()
    ).hexdigest()
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / f"{key}.json"
    if path.is_file():
        receipt = json.loads(path.read_text())
        if receipt.get("complete"):
            return {**receipt, "cached": True}
    receipt = {"request": request, "conditions": [], "cached": False}
    for label, data in (
        ("recorded", original),
        ("changed", original + plan.append.encode()),
    ):
        condition = {
            "label": label,
            "input_sha256": hashlib.sha256(data).hexdigest(),
            "input_bytes": len(data),
            "input_tail": repr(data[-200:]),
            "commands": [],
        }
        receipt["conditions"].append(condition)
        for argv in plan.commands:
            condition["commands"].append(
                _command(plan.image, plan.mount_path, data, argv)
            )
            path.write_text(json.dumps(receipt, indent=1) + "\n")
    receipt["complete"] = all(
        c["resources_removed"] and c["returncode"] not in {125, 126, 127}
        for condition in receipt["conditions"]
        for c in condition["commands"]
    )
    path.write_text(json.dumps(receipt, indent=1) + "\n")
    return receipt
