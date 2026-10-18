"""Second stage of scripts/firecracker-base.sh: finish the base image in Firecracker.

  python -m nika.firecracker.base <build_dir> <kernel_version>

Boots ``<build_dir>/rootfs.img`` (prepared by the script), runs
``scripts/install.sh --skip-images`` as the guest's ubuntu user, and fills the
guest's Docker with this host's runtime images: digest-pinned images are pulled
by the guest (keeping their digests), locally built ones are copied over with
``docker save`` / ``docker load``. The guest reaches the network only through
a host proxy over vsock. Writes ``base.json`` on success.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nika.config import REPO_ROOT
from nika.firecracker.config import firecracker_settings
from nika.firecracker.image import GUEST_REPO, PROXY_PORT
from nika.firecracker.vmm import (
    DISK,
    EXEC_PORT,
    EXIT_MARK,
    VSOCK,
    Firecracker,
    HostProxy,
    VmmError,
    connect_vsock,
    firecracker_binary,
    guest_exec,
)

BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 root=LABEL=nika-root rw "
    "cloud-init=disabled systemd.mask=systemd-networkd-wait-online.service"
)
DOCKER_PROXY = "/etc/systemd/system/docker.service.d/nika-build-proxy.conf"
# Caches under .nika_cache that k8s labs deploy from (filled by `nika images prepare`).
CACHE_DIRS = ("k8s-images", "helm")


def _log(message: str) -> None:
    print(f"+ {message}", flush=True)


def _wait_exec(uds: Path, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            guest_exec(uds, "true", timeout=10)
            return
        except (OSError, VmmError):
            time.sleep(1)
    raise TimeoutError("guest exec did not come up")


def _stream_file(uds: Path, script: str, path: str) -> None:
    """Run ``script`` in the guest with the contents of host file ``path`` as stdin."""
    import base64

    size = os.path.getsize(path)
    sock = connect_vsock(uds, EXEC_PORT)
    try:
        sock.sendall(base64.b64encode(script.encode()) + f"\n{size}\n".encode())
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                sock.sendall(chunk)
        out = bytearray()
        while chunk := sock.recv(1 << 16):
            out += chunk
    finally:
        sock.close()
    body, _, tail = bytes(out).rpartition(EXIT_MARK)
    if tail.split(b"\n", 1)[0].strip() != b"0":
        raise VmmError(f"{script} failed: {body.decode(errors='replace')[-500:]}")


def _load_image(uds: Path, image: str) -> None:
    """Stream ``docker save image`` into the guest's ``docker load``."""
    with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
        subprocess.run(["docker", "save", "-o", tar.name, image], check=True)
        _stream_file(uds, "docker load -q", tar.name)


def _copy_caches(uds: Path) -> None:
    """Copy this host's k8s image and Helm chart caches into the guest checkout.

    k8s labs load their in-cluster images and charts from ``.nika_cache``;
    ``nika images prepare`` fills it on this host.
    """
    caches = [
        name for name in CACHE_DIRS if (REPO_ROOT / ".nika_cache" / name).is_dir()
    ]
    if not caches:
        return
    with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
        subprocess.run(
            ["tar", "-cf", tar.name, "-C", str(REPO_ROOT / ".nika_cache"), *caches],
            check=True,
        )
        _stream_file(
            uds,
            f"mkdir -p {GUEST_REPO}/.nika_cache && tar -xf - -C {GUEST_REPO}/.nika_cache "
            f"&& chown -R ubuntu:ubuntu {GUEST_REPO}/.nika_cache",
            tar.name,
        )


def _host_plugins() -> list[str]:
    """Kathara network plugins enabled in this host's Docker."""
    out = subprocess.run(
        ["docker", "plugin", "ls", "--format", "{{.Name}} {{.Enabled}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [
        line.split()[0]
        for line in out
        if line.startswith("kathara/katharanp") and line.endswith("true")
    ]


def build(build_dir: Path, kernel_version: str) -> None:
    settings = firecracker_settings()
    disk = build_dir / DISK
    disk.unlink(missing_ok=True)
    disk.symlink_to("rootfs.img")
    vm = Firecracker(build_dir, firecracker_binary(settings.binary))
    vm.start()
    proxy = HostProxy(build_dir, PROXY_PORT)
    try:
        vm.boot(
            kernel=str(build_dir / "vmlinux"),
            initrd=str(build_dir / "initrd.img"),
            boot_args=BOOT_ARGS,
            cpus=settings.vm_cpus,
            memory_mib=8192,
            huge_pages=False,
        )
        uds = build_dir / VSOCK
        _wait_exec(uds)
        _log("guest is up; installing NIKA (scripts/install.sh --skip-images)")
        guest_exec(
            uds,
            proxy.guest_env()
            + f"cd {GUEST_REPO} && su ubuntu -c 'HTTPS_PROXY=$HTTPS_PROXY "
            "HTTP_PROXY=$HTTP_PROXY ./scripts/install.sh --skip-images' >/tmp/install.log 2>&1 "
            "|| { tail -40 /tmp/install.log; exit 1; }",
        )
        guest_exec(
            uds,
            f"cd {GUEST_REPO} && [ -f config/nika.yaml ] || cp config/nika.example.yaml "
            "config/nika.yaml; sed -i 's/gateway_port: *0/gateway_port: 8800/' "
            "config/nika.yaml; chown ubuntu:ubuntu config/nika.yaml",
        )
        images = subprocess.run(
            [str(Path(sys.executable).parent / "nika"), "images", "list"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        local = [ref for ref in images if "@sha256:" not in ref]
        # NIKA rebuilds a local image whose build hash (which covers its
        # parents' image IDs) does not match, so its parents must be present.
        from nika.net_env.utils.kathara.docker_files.docker_images import (
            dockerfile_parents,
        )

        parents = {
            p for ref in local for p in dockerfile_parents(ref) if "@sha256:" in p
        }
        pinned = sorted({ref for ref in images if "@sha256:" in ref} | parents)
        _log(f"pulling {len(pinned)} pinned image(s) through the host proxy")
        guest_exec(
            uds,
            f"mkdir -p {Path(DOCKER_PROXY).parent} && printf '[Service]\\n"
            f"Environment=HTTPS_PROXY=http://127.0.0.1:{PROXY_PORT} "
            f"HTTP_PROXY=http://127.0.0.1:{PROXY_PORT}\\n' > {DOCKER_PROXY} && "
            "systemctl daemon-reload && systemctl restart docker && "
            f"systemd-run --unit nika-build-proxy socat TCP-LISTEN:{PROXY_PORT},"
            f"bind=127.0.0.1,fork,reuseaddr VSOCK-CONNECT:2:{PROXY_PORT}",
        )
        for ref in pinned:
            _log(f"  docker pull {ref}")
            guest_exec(uds, f"docker pull -q {ref}")
        # Kathara attaches lab links through this Docker plugin; Docker would
        # otherwise fetch it on first use, and lab images have no network.
        plugins = _host_plugins()
        for plugin in plugins:
            _log(f"  docker plugin install {plugin}")
            guest_exec(uds, f"docker plugin install --grant-all-permissions {plugin}")
        _log("copying the k8s image and Helm chart caches from this host")
        _copy_caches(uds)
        _log(f"copying {len(local)} locally built image(s) from this host")
        for ref in local:
            _log(f"  {ref}")
            _load_image(uds, ref)
        missing = guest_exec(
            uds,
            f"rm -f {DOCKER_PROXY}; systemctl stop nika-build-proxy; "
            "systemctl daemon-reload; systemctl restart docker; "
            + "; ".join(
                [
                    f"docker image inspect {r} >/dev/null 2>&1 || echo {r}"
                    for r in images
                ]
                + [
                    f"docker plugin inspect {p} >/dev/null 2>&1 || echo {p}"
                    for p in plugins
                ]
            ),
        ).split()
        if missing:
            raise VmmError(f"images or plugins missing in the base image: {missing}")
        guest_exec(uds, "systemctl enable nika-remote >/dev/null 2>&1; sync")
    finally:
        proxy.close()
        vm.kill()
        disk.unlink(missing_ok=True)
        for name in (VSOCK, "api.sock"):
            (build_dir / name).unlink(missing_ok=True)
    (build_dir / "base.json").write_text(
        json.dumps(
            {
                "id": f"{time.strftime('%Y%m%d%H%M%S')}-{socket.gethostname()}-{kernel_version}",
                "rootfs": "rootfs.img",
                "kernel": "vmlinux",
                "initrd": "initrd.img",
                "boot_args": BOOT_ARGS,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]), sys.argv[2])
