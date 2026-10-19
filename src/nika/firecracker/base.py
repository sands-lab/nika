"""The base image every lab image starts from, built automatically on first use.

:func:`ensure_base` builds ``<root>/base/`` when it is missing or its key (the
builder version, this host's runtime images and Kathara plugins) changed:

1. **Root filesystem, with Docker** (no root needed): an ``ubuntu:24.04``
   container installs the kernel, systemd, Docker and the guest services and
   receives this checkout; its filesystem is exported and written into an ext4
   image with ``mkfs.ext4 -d`` in a second container.
2. **In Firecracker:** the image boots, runs ``scripts/install.sh
   --skip-images`` as the guest's ubuntu user, and fills the guest's Docker with
   this host's runtime images: digest-pinned images are pulled by the guest
   (keeping their digests), locally built ones are copied with ``docker save`` /
   ``docker load``, plus the Kathara plugin and the k8s caches. The guest reaches
   the network only through a host proxy over vsock.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from nika.config import REPO_ROOT
from nika.firecracker.config import firecracker_settings
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

GUEST_REPO = "/home/ubuntu/nika"
SYNCED_FILES = ".nika-files"  # files of the last code sync, NUL-separated
PROXY_PORT = 3128  # host CONNECT proxy, reached over vsock while building
# Bump when the base image's contents change, to rebuild existing bases.
BASE_VERSION = 1
UBUNTU_IMAGE = "ubuntu:24.04"
ROOTFS_SIZE = "80G"
BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 root=LABEL=nika-root rw "
    "cloud-init=disabled systemd.mask=systemd-networkd-wait-online.service"
)
DOCKER_PROXY = "/etc/systemd/system/docker.service.d/nika-build-proxy.conf"
# Caches under .nika_cache that k8s labs deploy from (filled by `nika images prepare`).
CACHE_DIRS = ("k8s-images", "helm")


def _repo_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p and (repo / p).is_file())


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


def _finish_in_firecracker(build_dir: Path, kernel_version: str, key: str) -> None:
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
        images = _runtime_images()
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
                "key": key,
                "rootfs": "rootfs.img",
                "kernel": "vmlinux",
                "initrd": "initrd.img",
                "boot_args": BOOT_ARGS,
            },
            indent=1,
        )
    )


GUEST_SETUP = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends systemd systemd-sysv udev dbus kmod \
  linux-image-generic initramfs-tools ca-certificates curl git socat sudo iproute2 \
  iptables iputils-ping python3 e2fsprogs >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin >/dev/null
apt-get clean
id ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash ubuntu
usermod -aG docker,sudo ubuntu
echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-ubuntu
echo nika-lab > /etc/hostname
echo 'LABEL=nika-root / ext4 defaults,noatime 0 1' > /etc/fstab
cat > /usr/local/bin/nika-guest-exec <<'EOF'
#!/bin/bash
# One command per vsock connection: a base64 script line, a byte-count line,
# then that many bytes of stdin for the script. Output ends with the exit status.
read -r script
read -r size
head -c "$size" | bash -c "$(printf '%s' "$script" | base64 -d)" 2>&1
printf '\n__nika_exit=%d\n' "${PIPESTATUS[1]}"
EOF
chmod 755 /usr/local/bin/nika-guest-exec
unit() { cat > "/etc/systemd/system/$1.service"; systemctl enable "$1" >/dev/null 2>&1; }
unit nika-guest-exec <<'EOF'
[Unit]
Description=NIKA guest exec on vsock 9000 (reached only through the VM's host-side socket)
[Service]
ExecStart=/usr/bin/socat VSOCK-LISTEN:9000,fork,reuseaddr EXEC:/usr/local/bin/nika-guest-exec
Restart=always
[Install]
WantedBy=multi-user.target
EOF
unit nika-remote <<'EOF'
[Unit]
Description=NIKA remote daemon (loopback only; reached over vsock)
After=docker.service
Requires=docker.service
ConditionPathExists=/home/ubuntu/nika/.venv/bin/nika
[Service]
User=ubuntu
Group=docker
WorkingDirectory=/home/ubuntu/nika
ExecStart=/home/ubuntu/nika/.venv/bin/nika remote serve --host 127.0.0.1 --port 8700
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
for port in 8700 8800; do
  unit nika-vsock-$port <<EOF
[Unit]
Description=Expose loopback port $port on vsock port $port
[Service]
ExecStart=/usr/bin/socat VSOCK-LISTEN:$port,fork,reuseaddr TCP:127.0.0.1:$port
Restart=always
[Install]
WantedBy=multi-user.target
EOF
done
# Keep a restored lab's clock on its image's timeline and drop idle services.
systemctl mask systemd-timesyncd.service apt-daily.timer apt-daily-upgrade.timer \
  dpkg-db-backup.timer e2scrub_all.timer fstrim.timer logrotate.timer \
  systemd-networkd-wait-online.service >/dev/null 2>&1 || true
ls /boot/initrd.img-* >/dev/null
"""

# Runs in a throwaway container: the builder's exported filesystem arrives on
# stdin and becomes /out/rootfs.img; the kernel and initrd are copied next to it.
PACK_ROOTFS = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends e2fsprogs zstd \
  xz-utils lz4 bzip2 binutils >/dev/null
mkdir /stage && tar -x -C /stage
printf '127.0.0.1 localhost\n127.0.1.1 nika-lab\n' > /stage/etc/hosts
: > /stage/etc/resolv.conf
: > /stage/etc/machine-id
kver=$(ls /stage/lib/modules | sort -V | tail -1)
sh /x/extract-vmlinux /stage/boot/vmlinuz-$kver > /out/vmlinux
cp /stage/boot/initrd.img-$kver /out/initrd.img
mkfs.ext4 -q -L nika-root -d /stage /out/rootfs.img {size}
chown {uid}:{gid} /out/vmlinux /out/initrd.img /out/rootfs.img
echo $kver
"""


def _docker(*args: str, stdin=None, stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], stdin=stdin, stdout=stdout, check=True)


def _extract_vmlinux(cache: Path) -> Path:
    local = Path(f"/usr/src/linux-headers-{os.uname().release}/scripts/extract-vmlinux")
    if local.is_file():
        return local
    path = cache / "extract-vmlinux"
    if not path.is_file():
        import urllib.request

        cache.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/torvalds/linux/master/scripts/extract-vmlinux",
            path,
        )
    return path


def _build_rootfs(build_dir: Path) -> str:
    """Write rootfs.img, vmlinux and initrd.img into ``build_dir``; return the kernel version."""
    name = f"nika-firecracker-base-{uuid.uuid4().hex[:8]}"
    _docker("pull", "-q", UBUNTU_IMAGE, stdout=subprocess.DEVNULL)
    _docker(
        "run",
        "-d",
        "--name",
        name,
        UBUNTU_IMAGE,
        "sleep",
        "infinity",
        stdout=subprocess.DEVNULL,
    )
    try:
        _log("installing the kernel, Docker and the guest services (container)")
        subprocess.run(
            ["docker", "exec", "-i", name, "bash", "-s"],
            input=GUEST_SETUP.encode(),
            check=True,
        )
        _log("copying this checkout")
        files = _repo_files(REPO_ROOT)
        with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
            subprocess.run(
                ["tar", "-cf", tar.name, "-C", str(REPO_ROOT), "--null", "-T", "-"],
                input="\0".join(files).encode(),
                check=True,
            )
            with open(tar.name, "rb") as fh:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "bash",
                        "-c",
                        f"mkdir -p {GUEST_REPO} && tar -x -C {GUEST_REPO}",
                    ],
                    stdin=fh,
                    check=True,
                )
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                name,
                "bash",
                "-c",
                f"cat > {GUEST_REPO}/{SYNCED_FILES} && "
                f"chown -R ubuntu:ubuntu {GUEST_REPO}",
            ],
            input="\0".join(files).encode(),
            check=True,
        )
        _log("writing the ext4 root filesystem")
        export = subprocess.Popen(["docker", "export", name], stdout=subprocess.PIPE)
        extract = _extract_vmlinux(REPO_ROOT / ".nika_cache" / "firecracker")
        script = PACK_ROOTFS.format(size=ROOTFS_SIZE, uid=os.getuid(), gid=os.getgid())
        packed = subprocess.run(
            [
                "docker",
                "run",
                "-i",
                "--rm",
                "-v",
                f"{build_dir}:/out",
                "-v",
                f"{extract}:/x/extract-vmlinux:ro",
                UBUNTU_IMAGE,
                "bash",
                "-c",
                script,
            ],
            stdin=export.stdout,
            stdout=subprocess.PIPE,
            check=True,
        )
        export.stdout.close()
        if export.wait() != 0:
            raise VmmError("docker export of the base container failed")
        return packed.stdout.decode().split()[-1]
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _runtime_images() -> list[str]:
    return subprocess.run(
        [str(Path(sys.executable).parent / "nika"), "images", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


@functools.cache
def base_key() -> str:
    """What the base image must match: builder version, runtime images, plugins."""
    images = []
    for ref in _runtime_images():
        image_id = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Id}}", ref],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        images.append(f"{ref}={image_id}")
    payload = {
        "version": BASE_VERSION,
        "ubuntu": UBUNTU_IMAGE,
        "images": images,
        "plugins": _host_plugins(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def _current(base: Path) -> bool:
    try:
        return json.loads((base / "base.json").read_text()).get("key") == base_key()
    except (OSError, ValueError):
        return False


def ensure_base() -> Path:
    """Return ``<root>/base``, building it first when missing or outdated."""
    root = Path(firecracker_settings().root)
    base = root / "base"
    if _current(base):
        return base
    locks = root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / "base.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _current(base):  # another process built it meanwhile
            return base
        print(
            "[firecracker] building the base image (first run, or the runtime "
            "images changed; about 20 minutes)",
            flush=True,
        )
        started = time.monotonic()
        work = root / "base.build"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        kernel = _build_rootfs(work)
        _finish_in_firecracker(work, kernel, base_key())
        old = root / "base.old"
        shutil.rmtree(old, ignore_errors=True)
        if base.exists():
            base.rename(old)  # lab images built from it keep their own disks
        work.rename(base)
        shutil.rmtree(old, ignore_errors=True)
        print(
            f"[firecracker] base image ready in {time.monotonic() - started:.0f}s",
            flush=True,
        )
    return base
