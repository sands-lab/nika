"""Run the pinned netlab compiler and collect per-node configuration text."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

NETLAB_VERSION = "26.10"


class _SrlLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [key.value for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate YAML fields in netlab SR Linux fragment")
        return super().construct_mapping(node, deep=deep)


def run_netlab(topology: dict, directory: Path) -> tuple[dict[str, str], dict]:
    """Compile a topology offline and return node configs plus transformed data."""
    try:
        installed = version("networklab")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Install the construction tool with uv sync --extra netlab"
        ) from exc
    if installed != NETLAB_VERSION:
        raise RuntimeError(f"Expected networklab=={NETLAB_VERSION}, found {installed}")

    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "topology.yml").write_text(yaml.safe_dump(topology, sort_keys=False))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from netsim.cli import main; main()",
            "create",
            "topology.yml",
            "-o",
            "config",
            "-o",
            "json=transformed.json",
        ],
        cwd=directory,
        env={k: v for k, v in os.environ.items() if not k.startswith("NETLAB_")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    (directory / "netlab.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"netlab compilation failed; see {directory / 'netlab.log'}")

    transformed = json.loads((directory / "transformed.json").read_text())
    configs = {}
    for name, node in transformed["nodes"].items():
        root = directory / "node_files" / name
        modules = ["initial", *node["module"]]
        modules += node.get("config", [])
        if node["device"] == "srlinux":
            operations = []
            for module in modules:
                document = yaml.load((root / module).read_text(), Loader=_SrlLoader)
                if not isinstance(document, dict):
                    raise ValueError(f"Unsupported netlab SRL fragment {module!r}")
                for action, entries in document.items():
                    if action not in {"updates", "replace"}:
                        raise ValueError(
                            f"Unsupported netlab SRL operation {action!r} in {module!r}"
                        )
                    if not isinstance(entries, list):
                        raise ValueError(f"SRL {action} in {module!r} must be a list")
                    for entry in entries:
                        if not isinstance(entry, dict) or set(entry) != {
                            "path",
                            "value",
                        }:
                            raise ValueError(
                                "SR Linux operations must contain exactly path and value"
                            )
                        operations.append(
                            {
                                "operation": "update"
                                if action == "updates"
                                else "replace",
                                **entry,
                            }
                        )
            configs[name] = yaml.safe_dump({"operations": operations}, sort_keys=False)
            continue
        fragments = []
        for module in modules:
            fragment = (root / module).read_text()
            if node["device"] == "frr" and module == "initial":
                # Upstream packages FRR initial CLI in a shell heredoc. Extract
                # the literal CLI; NIKA already owns Linux interface creation.
                marker = "cat >/tmp/config <<CONFIG\n"
                if fragment.count(marker) != 1:
                    raise ValueError("Unsupported netlab FRR initial config envelope")
                fragment = fragment.split(marker)[1].split("\nCONFIG\n", 1)[0]
            if node["device"] == "routeros7" and module == "bgp":
                # netlab 26.10 uses three invalid community property spellings.
                # RouterOS Route Selection and Filters documents these names.
                fragment = (
                    fragment.replace("bgp-lrg-communities", "bgp-large-communities")
                    .replace("bgp-std-communities", "bgp-communities")
                    .replace(
                        "delete bgp-ext-communities wk;", "delete bgp-communities wk;"
                    )
                )
                if "local.address=loopback" in fragment:
                    source = node["loopback"]["ipv4"].split("/")[0]
                    fragment = fragment.replace(
                        "local.address=loopback", "local.address=" + source
                    )
            # 'do write' is an interactive persistence command, not startup CLI.
            fragments.append(
                "\n".join(
                    line for line in fragment.splitlines() if line.strip() != "do write"
                )
            )
        separator = "\n" if node["device"] == "routeros7" else "\n!\n"
        configs[name] = separator.join(fragments) + "\n"
    return configs, transformed
