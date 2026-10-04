"""Compress a local batch with bounded output storage and an explicit worker limit."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import time

ROOT = Path("/dev/shm/archive-job")
CONFIG = Path("/etc/archive-job.json")
PIDFILE = Path("/run/archive-job.pid")


def running_pid():
    try:
        pid = int(PIDFILE.read_text())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if b"/usr/local/bin/archive-job.py" in command and os.getpgid(pid) == pid:
            return pid
    except (OSError, ValueError):
        pass
    return None


def status():
    pid = running_pid()
    active = 0
    if pid:
        for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split():
            try:
                active += Path(f"/proc/{child}/comm").read_text().strip() == "gzip"
            except FileNotFoundError:
                pass
    resources = {}
    for name in ("cpu.stat", "cpu.pressure", "io.pressure"):
        path = Path("/sys/fs/cgroup") / name
        if path.exists():
            resources[name] = path.read_text()
    return {
        "pid": pid,
        "active_workers": active,
        **json.loads(CONFIG.read_text()),
        "resources": resources,
    }


def run(duration):
    with PIDFILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        workers = int(json.loads(CONFIG.read_text())["workers"])
        if not 1 <= workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        children = {}
        completed = 0
        deadline = time.monotonic() + duration

        def finish(_signum, _frame):
            raise SystemExit

        signal.signal(signal.SIGTERM, finish)
        try:
            # Each worker archives the next snapshot of the local corpus. Keep
            # its latest result so the batch cannot fill the filesystem.
            while time.monotonic() < deadline:
                for index in range(workers):
                    child = children.get(index)
                    if child is not None and child.poll() is None:
                        continue
                    if child is not None:
                        if child.returncode:
                            raise RuntimeError(f"gzip exited with {child.returncode}")
                        completed += 1
                    with (ROOT / f"snapshot-{index}.gz").open("wb") as output:
                        children[index] = subprocess.Popen(
                            ["gzip", "-n", "-6", "-c", str(ROOT / "input.dat")],
                            stdout=output,
                        )
                time.sleep(0.02)
        finally:
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            for child in children.values():
                child.wait()
            lock.seek(0)
            lock.truncate()
            print(json.dumps({"completed_snapshots": completed}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "start", "run", "status", "stop"))
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.action == "prepare":
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "input.dat").write_bytes(random.Random(42).randbytes(2 * 1024 * 1024))
    elif args.action == "run":
        run(args.duration)
    elif args.action == "status":
        print(json.dumps(status()))
    elif args.action == "stop":
        pid = running_pid()
        if pid and args.pid is not None and pid != args.pid:
            raise SystemExit("archive job belongs to a different invocation")
        if pid:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(100):
                if running_pid() is None:
                    break
                time.sleep(0.05)
        print(json.dumps({"stopped": running_pid() is None}))
    elif args.action == "start":
        if running_pid():
            raise SystemExit("archive job is already running")
        with open("/var/log/archive-job.log", "a") as log:
            child = subprocess.Popen(
                ["python3", __file__, "run", "--duration", str(args.duration)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        for _ in range(100):
            if running_pid() == child.pid:
                print(json.dumps({"pid": child.pid}))
                break
            if child.poll() is not None:
                raise SystemExit("archive job failed to start")
            time.sleep(0.05)
        else:
            raise SystemExit("archive job startup timed out")
