"""Measure NetArena's healthy Kubernetes deployment in owned kind clusters.

Keep the upstream deployment function and report its return separately from
Deployment Available. No incident generator or diagnosis agent is imported.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

UPSTREAM = "525ca6a80fce5a8faef508b70137fbd0513a85cb"


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.checkout = args.checkout.resolve()
    args.out = args.out.resolve()
    from experiment.healthy_scenarios.sample import SCRIPT, sample

    commit = subprocess.check_output(
        ["git", "-C", str(args.checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != UPSTREAM:
        parser.error(f"Expected upstream {UPSTREAM}, found {commit}")
    source = args.checkout / "app-k8s/run_workflow.py"
    tree = ast.parse(source.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "deploy_k8s_cluster"
    )
    namespace = {"subprocess": subprocess, "logger": logging.getLogger("netarena")}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    manifests = (
        args.checkout / "app-k8s/microservices-demo/release/kubernetes-manifests.yaml"
    )
    sys.path.insert(0, str(args.checkout / "app-k8s"))
    from correct_policy import copy_yaml_to_new_folder
    from deploy_policies import deploy_policies, POLICY_NAMES

    args.out.mkdir(parents=True, exist_ok=True)
    original_kubeconfig = os.environ.get("KUBECONFIG")
    for index in range(1, 4):
        root = args.out / f"run_{index:02d}"
        root.mkdir()
        name = "nika-healthy-" + uuid.uuid4().hex[:10]
        kubeconfig = root.resolve() / "kubeconfig"
        os.environ["KUBECONFIG"] = str(kubeconfig)
        record = {
            "upstream": commit,
            "healthy_only": True,
            "cluster": name,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifests.read_bytes()).hexdigest(),
            "health_scope": "Deployment Available; no policy enforcement claim",
            "phases": [],
        }

        def timed(phase, function):
            started, epoch = time.monotonic_ns(), time.time_ns()
            try:
                return function()
            finally:
                record["phases"].append(
                    {
                        "phase": phase,
                        "start_epoch_ns": epoch,
                        "duration_ns": time.monotonic_ns() - started,
                    }
                )

        def command(*arguments):
            return subprocess.run(arguments, check=True, timeout=900)

        created = False
        try:
            created = True
            timed(
                "cluster_create",
                lambda: command(
                    "kind",
                    "create",
                    "cluster",
                    "--name",
                    name,
                    "--kubeconfig",
                    str(kubeconfig),
                    "--wait",
                    "180s",
                ),
            )
            timed(
                "upstream_app_deploy",
                lambda: namespace["deploy_k8s_cluster"](str(manifests.parent.parent)),
            )
            timed(
                "health_verification",
                lambda: command(
                    "kubectl",
                    "wait",
                    "--for=condition=Available",
                    "deployment",
                    "--all",
                    "--timeout=600s",
                ),
            )
            snapshot = subprocess.check_output(
                ["kubectl", "get", "pods", "-A", "-o", "json"], text=True
            )
            (root / "pods.json").write_text(snapshot)
            timed(
                "prepare_healthy_policies",
                lambda: copy_yaml_to_new_folder(
                    str(manifests.parent.parent), str(root)
                ),
            )
            timed(
                "apply_healthy_policies",
                lambda: deploy_policies(POLICY_NAMES, str(root)),
            )
            policies = json.loads(
                subprocess.check_output(
                    ["kubectl", "get", "networkpolicies", "-o", "json"], text=True
                )
            )
            if not set(POLICY_NAMES) <= {
                row["metadata"]["name"] for row in policies["items"]
            }:
                raise RuntimeError("Healthy policy objects were not installed")
            (root / "networkpolicies.json").write_text(json.dumps(policies, indent=2))
            ready = time.monotonic()

            def export():
                nodes = subprocess.check_output(
                    ["kind", "get", "nodes", "--name", name], text=True
                ).splitlines()
                state = {}
                for node in nodes:
                    state[node] = {
                        "raw": subprocess.check_output(
                            ["docker", "exec", node, "sh", "-c", SCRIPT],
                            text=True,
                            timeout=300,
                        )
                    }
                state["cluster"] = {
                    "raw": "@@@ pods\n"
                    + subprocess.check_output(
                        ["kubectl", "get", "pods", "-A", "-o", "json"], text=True
                    )
                }
                return state

            sample(root, ready, export)
            record["status"] = "complete"
        except BaseException:
            record.update(status="failed", error=traceback.format_exc())
            raise
        finally:
            try:
                if created:
                    timed(
                        "cluster_delete",
                        lambda: command("kind", "delete", "cluster", "--name", name),
                    )
            except BaseException:
                record["cleanup_error"] = traceback.format_exc()
                raise
            finally:
                (root / "result.json").write_text(json.dumps(record, indent=2))
                if original_kubeconfig is None:
                    os.environ.pop("KUBECONFIG", None)
                else:
                    os.environ["KUBECONFIG"] = original_kubeconfig


if __name__ == "__main__":
    main()
