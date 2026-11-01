"""SONiC-VS image, interface naming and boot sequence on Containerlab.

NIKA runs the official ``docker-sonic-vs`` image from the SONiC build
pipeline. Containerlab's ``sonic-vs`` kind reserves container ``eth0`` for
management and starts supervisord after the lab's links exist. The virtual
switch SAI maps container data interface ``eth{k}`` to SONiC port
``Ethernet{4(k-1)}`` through the lane map NIKA writes for each node.

SONiC routes with FRR (``vtysh``) and forwards in the Linux kernel, so FRR
designs keep their semantics: CONFIG_DB holds ports and addresses, and FRR
holds the routing protocols.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path

from nika.net_env.utils.kathara.docker_files.docker_images import image_exists

# SONiC 202505 virtual switch, from the sonic-buildimage pipeline build below.
SONIC_BRANCH = "202505"
SONIC_BUILD_ID = 1238524
IMAGE = f"docker-sonic-vs:{SONIC_BRANCH}-{SONIC_BUILD_ID}"
ARTIFACT_URL = (
    "https://sonic-build.azurewebsites.net/api/sonic/artifacts"
    f"?branchName={SONIC_BRANCH}&platform=vs&buildId={SONIC_BUILD_ID}"
    "&target=target/docker-sonic-vs.gz"
)
ARTIFACT_SHA256 = "40697b8c16096d418490dc59f571aaa3d35a0472c21a5e823c9b5a32c1a91005"

KIND = "sonic-vs"
PLATFORM = "x86_64-kvm_x86_64-r0"
HWSKU = "Force10-S6000"
HWSKU_DIR = f"/usr/share/sonic/device/{PLATFORM}/{HWSKU}"
# Read-only bind of the node's generated files.
CONFIG_DIR = "/etc/sonic/startup"
PORT_MTU = 9100
# Memory one SONiC router needs, with headroom: Firecracker lab images of the
# SNDlib ISPs on SONiC use 2.4-2.9 GiB for 10-11 routers and as many hosts.
ROUTER_MEMORY_MIB = 256
# CPU one SONiC router needs, millicores, with headroom. isp_pdh's 11 routers
# idle near 0.3 cores and hold 2.5-3.5 cores after a short start spike.
ROUTER_CPU_MILLIS = 250

BOOT_TIMEOUT_SEC = 900

_CONTAINER_IFACE = re.compile(r"eth([1-9][0-9]*)")
_PORT = re.compile(r"Ethernet(0|[1-9][0-9]*)")


def sonic_port(container_iface: str) -> str:
    """Map container data interface ``eth{k}`` to SONiC port ``Ethernet{4(k-1)}``."""
    match = _CONTAINER_IFACE.fullmatch(container_iface)
    if not match:
        raise ValueError(f"Not a SONiC-VS data interface: {container_iface!r}")
    return f"Ethernet{4 * (int(match[1]) - 1)}"


def container_iface(port: str) -> str:
    """Invert :func:`sonic_port`."""
    match = _PORT.fullmatch(port)
    if not match or int(match[1]) % 4:
        raise ValueError(f"Not a SONiC-VS port: {port!r}")
    return f"eth{int(match[1]) // 4 + 1}"


def _loaded_image_ref(stdout: str) -> str | None:
    """Return the image name or ID ``docker load`` reported."""
    for line in stdout.splitlines():
        if line.startswith("Loaded image ID:"):
            return line.removeprefix("Loaded image ID:").strip()
        if line.startswith("Loaded image:"):
            return line.removeprefix("Loaded image:").strip()
    return None


def ensure_sonic_image(image: str = IMAGE) -> None:
    """Download, verify and load the pinned SONiC-VS image when it is missing."""
    if image_exists(image):
        return
    from nika.config import CACHE_DIR

    cache = CACHE_DIR / "vendor" / "sonic"
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "image.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            _load_sonic_image(cache, image)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load_sonic_image(cache: Path, image: str) -> None:
    if image_exists(image):
        return
    print(f"Downloading SONiC-VS {SONIC_BRANCH} build {SONIC_BUILD_ID} ...")
    with tempfile.NamedTemporaryFile(dir=cache, suffix=".gz", delete=False) as handle:
        archive = Path(handle.name)
    try:
        digest = hashlib.sha256()
        with archive.open("wb") as handle:
            with urllib.request.urlopen(ARTIFACT_URL, timeout=60) as response:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    handle.write(chunk)
        if digest.hexdigest() != ARTIFACT_SHA256:
            raise RuntimeError(
                f"SONiC-VS artifact sha256 {digest.hexdigest()} does not match "
                f"the pinned {ARTIFACT_SHA256}; download it from {ARTIFACT_URL}."
            )
        loaded = subprocess.run(
            ["docker", "load", "-i", str(archive)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if image_exists(image):
            return
        source = _loaded_image_ref(loaded)
        if not source:
            raise RuntimeError(f"docker load did not report an image:\n{loaded}")
        if source != image:
            subprocess.run(["docker", "tag", source, image], check=True)
    finally:
        archive.unlink(missing_ok=True)


# Services that must run once SONiC's start.sh has finished.
BOOT_SERVICES = ("orchagent", "syncd", "zebra", "fpmsyncd")
# Supervisord states that mean a boot service has stopped after start.sh.
_BOOT_FAILED = frozenset({"FATAL", "STOPPED", "EXITED"})


def _boot_state(runtime, node: str) -> str:
    """Return ``booting``, ``ready`` or ``failed``.

    The image's start.sh exits non-zero after starting every service because
    its last command names a program this build does not define, so readiness
    is the services running, not the script's exit status. STARTING, BACKOFF,
    and a missing status are still boot; FATAL, STOPPED, and EXITED are not.
    """
    output = runtime.exec(
        node, "supervisorctl status start.sh " + " ".join(BOOT_SERVICES), timeout=20
    )
    states = dict(
        line.split()[:2]
        for line in (output or "").splitlines()
        if len(line.split()) > 1
    )
    if states.get("start.sh") != "EXITED":
        return "booting"
    pending = False
    for service in BOOT_SERVICES:
        state = states.get(service)
        if state == "RUNNING":
            continue
        if state in _BOOT_FAILED:
            return "failed"
        pending = True
    return "booting" if pending else "ready"


def activate_sonic_routers(
    runtime,
    daemons: Mapping[str, Iterable[str]],
    *,
    timeout: float = BOOT_TIMEOUT_SEC,
) -> None:
    """Wait for SONiC to boot on each router, then start FRR with its design.

    ``daemons`` names, per router, the FRR daemons beyond zebra and staticd
    (which SONiC starts) that the router's ``frr.conf`` needs.
    """
    from nika.utils.parallel import bounded_parallel_map

    deadline = time.monotonic() + timeout

    def activate(item) -> str | None:
        node, needed = item
        while (state := _boot_state(runtime, node)) != "ready":
            if state == "failed" or time.monotonic() > deadline:
                log = runtime.exec(node, "tail -20 /var/log/syslog", timeout=20)
                return f"{node}: SONiC boot {state}\n{log}"
            time.sleep(3)
        output = runtime.exec(
            node,
            f"supervisorctl start {' '.join(sorted(set(needed)))} && "
            "vtysh -b > /tmp/frr-boot.log 2>&1; "
            "echo rc=$?; grep -iE 'error|unknown|warning' /tmp/frr-boot.log",
            timeout=120,
        )
        if "rc=0" not in output:
            return f"{node}: FRR configuration failed\n{output}"
        return None

    errors = [
        error
        for error in bounded_parallel_map(activate, sorted(daemons.items()))
        if error
    ]
    if errors:
        raise RuntimeError("SONiC activation failed:\n" + "\n".join(errors))
