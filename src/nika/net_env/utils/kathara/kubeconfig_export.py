"""Shared helpers for exporting a host-reachable kubeconfig from k8s labs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def write_host_kubeconfig(
    *,
    instance: Any,
    controller_machine: Any,
    remote_kubeconfig_path: str,
    runtime_workdir: Path,
    port: int,
    metadata: dict[str, Any],
) -> Path:
    """Fetch controller kubeconfig, rewrite server to localhost:port, update metadata."""
    runtime_workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        instance.retrieve_files(controller_machine, remote_kubeconfig_path, tmp_dir)
        raw_path = os.path.join(tmp_dir, os.path.basename(remote_kubeconfig_path))
        with open(raw_path, encoding="utf-8") as f:
            raw = f.read()
    patched = raw.replace("127.0.0.1:6443", f"localhost:{port}")
    kubeconfig_path = runtime_workdir / "kubeconfig.yaml"
    with open(kubeconfig_path, "w", encoding="utf-8") as f:
        f.write(patched)
    metadata["k8s_controller_port"] = port
    metadata["kubeconfig_path"] = str(kubeconfig_path)
    return kubeconfig_path
