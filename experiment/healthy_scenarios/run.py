"""Run a fixed healthy deployment manifest sequentially on one host.

Each mode uses the same deployment parameters, three repeats, and sampling
delays. Results retain subprocess failures and the exact collector command.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def run_child(command: list[str], env: dict, log) -> int:
    process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        return process.wait()
    except BaseException:
        process.terminate()
        process.wait()
        raise


def require_complete(root: Path, modes: list[str]) -> None:
    variants = sorted(path.parent for path in root.glob("*/params.json"))
    if not variants:
        raise RuntimeError(f"No preceding measurements found: {root}")
    for variant in variants:
        for mode in modes:
            receipt = variant / f"{mode}-process.json"
            if (
                not receipt.exists()
                or json.loads(receipt.read_text()).get("status") != "complete"
            ):
                raise RuntimeError(f"Preceding measurement is incomplete: {receipt}")


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", action="append")
    parser.add_argument(
        "--runs",
        type=int,
        choices=[1, 3],
        default=3,
        help="One repeat for a transport preflight; three for the experiment",
    )
    parser.add_argument(
        "--after",
        type=Path,
        help="Wait for the host lock, then require this batch to have completed",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["native", "firecracker", "incus"],
        default=["native", "firecracker", "incus"],
    )
    parser.add_argument("--incus-base")
    args = parser.parse_args()
    source_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(source_root / "src"))
    from nika.config import REPO_ROOT
    from nika.run_config.loader import resolve_run_config_path

    if REPO_ROOT.resolve() != source_root:
        raise RuntimeError(f"The interpreter loaded another NIKA checkout: {REPO_ROOT}")
    config_path = resolve_run_config_path()
    if "incus" in args.modes and not args.incus_base:
        parser.error(
            "Incus measurements require an explicitly prepared instance/snapshot"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    variants = [
        v for v in manifest["variants"] if not args.variant or v["id"] in args.variant
    ]
    if args.variant and set(args.variant) != {v["id"] for v in variants}:
        parser.error("Requested variant is absent from the manifest")
    with Path("/tmp/nika-healthy-scenarios.lock").open("w") as lock:
        if args.after:
            print(f"Waiting for the preceding batch: {args.after}", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if args.after else fcntl.LOCK_NB))
        if args.after:
            require_complete(args.after, args.modes)
        print(f"Measuring {len(variants)} variants with {args.modes}", flush=True)
        for variant in variants:
            root = args.out / variant["id"]
            root.mkdir(parents=True, exist_ok=True)
            params = root / "params.json"
            params.write_text(json.dumps(variant["params"], indent=2))
            for mode in args.modes:
                print(f"{variant['id']} {mode}", flush=True)
                receipt = root / f"{mode}-process.json"
                if receipt.exists():
                    raise FileExistsError(f"Preserve the existing attempt: {receipt}")
                command = [
                    sys.executable,
                    "-m",
                    "experiment.healthy_scenarios.collect",
                    "--mode",
                    mode,
                    "--variant-json",
                    str(params.resolve()),
                    "--runs",
                    str(args.runs),
                    "--delays",
                    "0,60",
                    "--out",
                    str(root.resolve()),
                ]
                if mode == "incus":
                    command += ["--incus-base", args.incus_base]
                env = dict(
                    os.environ,
                    PYTHONPATH=os.pathsep.join(
                        [str(source_root / "src"), str(source_root)]
                    ),
                    NIKA_HEALTHY_PHASE_LOG=str(
                        (root / f"{mode}-phases.jsonl").resolve()
                    ),
                )
                from experiment.healthy_scenarios.cpu import CPU_ENV, mask_for_count

                mask = mask_for_count(variant["vm_cpus"])
                env[CPU_ENV] = mask
                if mode != "native":
                    preparation = [
                        sys.executable,
                        "-m",
                        "experiment.healthy_scenarios.prepare",
                        "--mode",
                        mode,
                        "--params",
                        str(params.resolve()),
                        "--receipt",
                        str((root / f"{mode}-build.json").resolve()),
                    ]
                    if mode == "incus":
                        preparation += ["--incus-base", args.incus_base]
                    with (root / f"{mode}-build.log").open("w") as log:
                        if run_child(preparation, env, log):
                            raise SystemExit(
                                f"Snapshot preparation failed: {root / f'{mode}-build.log'}"
                            )
                record = {
                    "command": command,
                    "source_root": str(source_root),
                    "source_commit": manifest["source_commit"],
                    "run_config_path": str(config_path),
                    "run_config_sha256": hashlib.sha256(
                        config_path.read_bytes()
                    ).hexdigest()
                    if config_path.exists()
                    else None,
                    "params": variant["params"],
                    "cpu_alignment": {
                        "logical_cpus": mask,
                        "count": variant["vm_cpus"],
                        "shared_daemon_cpu_accounted_separately": True,
                    },
                    "host": socket.gethostname(),
                    "kernel": platform.release(),
                    "boot_id": Path("/proc/sys/kernel/random/boot_id")
                    .read_text()
                    .strip(),
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "measurement_code_sha256": {
                        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(Path(__file__).parent.glob("*.py"))
                    },
                    "meminfo_before": Path("/proc/meminfo").read_text(),
                    "started_epoch": time.time(),
                    "status": "running",
                }
                receipt.write_text(json.dumps(record, indent=2))
                with (root / f"{mode}.log").open("w") as log:
                    try:
                        returncode = run_child(command, env, log)
                    except BaseException:
                        record.update(status="interrupted", finished_epoch=time.time())
                        receipt.write_text(json.dumps(record, indent=2))
                        raise
                record.update(
                    returncode=returncode,
                    finished_epoch=time.time(),
                    status="complete" if returncode == 0 else "failed",
                )
                receipt.write_text(json.dumps(record, indent=2))
                if returncode:
                    raise SystemExit(
                        f"Check the retained failure before continuing: {receipt}"
                    )


if __name__ == "__main__":
    main()
