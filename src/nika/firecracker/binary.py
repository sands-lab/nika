"""The Firecracker binary, built automatically on first use.

Upstream Firecracker snapshots lose the state of a hypervisor running inside the
guest, so a restored lab that runs VMs (vrnetlab RouterOS, QEMU with KVM)
crashes once a nested VM runs again. ``firecracker-nested-virt.patch`` saves it
for AMD SVM and Intel VMX: the vCPU's KVM nested state and the MSRs an L1
hypervisor depends on. :func:`ensure_firecracker` builds the patched release in
a Rust container (no host toolchain or root needed) into
``<root>/firecracker/<key>/``.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from nika.firecracker.config import firecracker_root

FIRECRACKER_VERSION = "v1.17.0"
FIRECRACKER_REPO = "https://github.com/firecracker-microvm/firecracker"
RUST_IMAGE = "rust:1.97-bookworm"  # the toolchain FIRECRACKER_VERSION pins
PATCH = Path(__file__).with_name("firecracker-nested-virt.patch")

# musl-gcc has no kernel headers; userfaultfd-sys needs linux/types.h.
BUILD = """set -e
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends git libseccomp-dev musl-tools \
    clang libclang-dev >/dev/null
git clone -q --depth 1 --branch "$VERSION" "$REPO" /src
cd /src
git apply /w/firecracker.patch
rustup target add "$TARGET" >/dev/null
export CFLAGS_${TARGET//-/_}="-idirafter /usr/include -idirafter /usr/include/$(uname -m)-linux-gnu"
cargo build -q --release -p firecracker --target "$TARGET"
cp "build/cargo_target/$TARGET/release/firecracker" /w/firecracker
chown "$OWNER" /w/firecracker
"""


@functools.cache
def build_key() -> str:
    payload = f"{FIRECRACKER_VERSION}\n{RUST_IMAGE}\n{platform.machine()}\n"
    return hashlib.sha256(payload.encode() + PATCH.read_bytes()).hexdigest()[:12]


def ensure_firecracker() -> Path:
    """Return the patched Firecracker binary, building it first when missing."""
    root = firecracker_root()
    out = root / "firecracker" / build_key()
    binary = out / "firecracker"
    if binary.exists():
        return binary
    locks = root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / "firecracker.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.exists():  # another process built it meanwhile
            return binary
        print(
            f"[firecracker] building Firecracker {FIRECRACKER_VERSION} with the "
            "nested virtualization patch (first run; about 3 minutes)",
            flush=True,
        )
        started = time.monotonic()
        work = out.with_name(f"{out.name}.build")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        shutil.copy(PATCH, work / "firecracker.patch")
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{work}:/w",
                "-e",
                f"VERSION={FIRECRACKER_VERSION}",
                "-e",
                f"REPO={FIRECRACKER_REPO}",
                "-e",
                f"TARGET={platform.machine()}-unknown-linux-musl",
                "-e",
                f"OWNER={os.getuid()}:{os.getgid()}",
                RUST_IMAGE,
                "bash",
                "-c",
                BUILD,
            ],
            check=True,
        )
        shutil.rmtree(out, ignore_errors=True)
        work.rename(out)
        print(
            f"[firecracker] Firecracker ready in {time.monotonic() - started:.0f}s",
            flush=True,
        )
    return binary


@functools.cache
def binary_id(path: str) -> str:
    """Identify a Firecracker binary: lab images restore only with the one that saved them."""
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()[:12]
