"""Linux process identities for controller workers that share the host PID space.

This module uses only the standard library. Controllers also ship its source
into helper containers that do not have NIKA installed.
"""

from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import select
import signal
import subprocess
import sys


def process_source() -> str:
    return Path(__file__).read_text()


def process_command(*args: str) -> list[str]:
    return ["python3", "-c", process_source() + "\nmain()\n", *args]


def process_identity(pid: int) -> list:
    if pid <= 1:
        raise ValueError("Controller worker PID must be greater than 1")
    # comm may contain spaces and closing parentheses; the remaining fields
    # start at field 3, so starttime (field 22) is index 19.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return [
        pid,
        int(fields[19]),
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    ]


def owned_pidfd(identity) -> int | None:
    """Pin a live task before checking its recorded birth identity."""
    if not isinstance(identity, list) or len(identity) != 3:
        return None
    pid, ticks, boot_id = identity
    if (
        type(pid) is not int
        or pid <= 1
        or type(ticks) is not int
        or not isinstance(boot_id, str)
    ):
        return None
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    except (AttributeError, NotImplementedError) as exc:
        raise RuntimeError(
            "Controller process cleanup requires Linux pidfd support"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.EINVAL:
            # A dead group leader's PID can remain allocated while its child
            # keeps the process group alive. It has no task to pin or signal.
            try:
                process_identity(pid)
            except (FileNotFoundError, ProcessLookupError):
                return None
        if exc.errno == errno.ENOSYS:
            raise RuntimeError(
                "Controller process cleanup requires Linux pidfd support"
            ) from exc
        raise
    try:
        if process_identity(pid) != identity or select.select([fd], [], [], 0)[0]:
            os.close(fd)
            return None
    except (FileNotFoundError, ProcessLookupError):
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def send_pidfd(fd: int, signum: int) -> None:
    try:
        signal.pidfd_send_signal(fd, signum)
    except (AttributeError, NotImplementedError) as exc:
        raise RuntimeError(
            "Controller process cleanup requires Linux pidfd signaling"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            raise RuntimeError(
                "Controller process cleanup requires Linux pidfd signaling"
            ) from exc
        raise


def signal_owned(identity, signum: int) -> bool:
    fd = owned_pidfd(identity)
    if fd is None:
        return False
    try:
        send_pidfd(fd, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def stop_owned(identity) -> None:
    """Stop the verified worker; never signal a numeric process group."""
    fd = owned_pidfd(identity)
    if fd is None:
        return
    try:
        send_pidfd(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 5)[0]:
            send_pidfd(fd, signal.SIGKILL)
            if not select.select([fd], [], [], 5)[0]:
                raise RuntimeError("Controller worker did not exit after SIGKILL")
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def read_identity(path: str):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return None


def run_in_node_netns(pid: int, command: list[str], *, mount: bool = False) -> int:
    """Pin the task and its namespaces; refuse the host network namespace."""
    identity = process_identity(pid)
    pidfd = owned_pidfd(identity)
    if pidfd is None:
        raise RuntimeError("Lab node exited before entering its network namespace")
    descriptors = []
    try:
        net = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
        descriptors.append(net)
        try:
            host_namespace = os.stat("/proc/1/ns/net").st_ino
        except OSError as exc:
            raise RuntimeError(
                "Cannot verify host network namespace; refusing lab operation"
            ) from exc
        if os.fstat(net).st_ino == host_namespace:
            raise RuntimeError("Refusing host network namespace for a lab operation")
        args = ["nsenter", f"--net=/proc/self/fd/{net}"]
        if mount:
            mnt = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
            descriptors.append(mnt)
            args.append(f"--mount=/proc/self/fd/{mnt}")
        if select.select([pidfd], [], [], 0)[0]:
            raise RuntimeError("Lab node exited while pinning its namespaces")
        return subprocess.run(
            [*args, "--", *command], pass_fds=descriptors, check=False
        ).returncode
    finally:
        for fd in descriptors:
            os.close(fd)
        os.close(pidfd)


def run_owned(path: str, command: list[str]) -> int:
    """Run one command with a birth identity and a cancellation tombstone."""
    record = Path(path)
    cancelled = Path(path + ".cancel")
    child = None
    try:
        if cancelled.exists():
            return 125
        child = subprocess.Popen(command)
        try:
            identity = process_identity(child.pid)
        except (FileNotFoundError, ProcessLookupError):
            return child.wait()
        temporary = Path(f"{path}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(identity))
        temporary.replace(record)
        if cancelled.exists():
            stop_owned(identity)
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        record.unlink(missing_ok=True)
        cancelled.unlink(missing_ok=True)


def main() -> None:
    operation, path, *args = sys.argv[1:]
    if operation == "run":
        raise SystemExit(run_owned(path, args))
    if operation == "cancel":
        Path(path + ".cancel").touch()
        stop_owned(read_identity(path))
        return
    if operation in {"netns", "netns-mount"}:
        raise SystemExit(
            run_in_node_netns(int(path), args, mount=operation == "netns-mount")
        )
    if operation == "record":
        temporary = Path(f"{path}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(process_identity(int(args[0]))))
        temporary.replace(path)
    elif operation == "running":
        if signal_owned(read_identity(path), 0):
            print("running")
    elif operation == "stop":
        identity = read_identity(path)
        stop_owned(identity)
        if read_identity(path) == identity:
            Path(path).unlink(missing_ok=True)
    else:
        raise ValueError(f"Unknown controller process operation: {operation}")
