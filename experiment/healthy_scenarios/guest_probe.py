"""Install the same lifecycle timers in this experiment's disposable guests."""

from __future__ import annotations

import base64
import functools
import hashlib
import os
import shlex
from pathlib import Path


def installation_command() -> str:
    from nika.firecracker.base import GUEST_REPO

    pth = (
        f"{GUEST_REPO}\n"
        "import os; os.environ['NIKA_HEALTHY_PHASE_LOG'] = "
        f"'{GUEST_REPO}/runtime/healthy-phases.jsonl'; "
        "os.environ['NIKA_HEALTHY_CLOCK_DOMAIN'] = 'guest'; "
        "from experiment.healthy_scenarios.phases import install; install()\n"
    )
    code = (
        "import pathlib, sysconfig; "
        f"pathlib.Path(sysconfig.get_path('purelib'), 'nika_healthy_probe.pth').write_text({pth!r})"
    )
    encoded = base64.b64encode(code.encode()).decode()
    return (
        f"cd {shlex.quote(GUEST_REPO)} && .venv/bin/python -c "
        + shlex.quote(f"import base64; exec(base64.b64decode({encoded!r}))")
        + " && systemctl restart nika-remote"
    )


def install() -> None:
    import nika.firecracker.image as image
    from nika.firecracker.vmm import guest_exec
    from experiment.incus import golden
    from experiment.incus.incus import guest

    original_fc = image.sync_code
    original_incus = golden.sync_code
    original_fingerprint = image.code_fingerprint
    observer_hash = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        observer_hash.update(path.name.encode())
        observer_hash.update(path.read_bytes())
    observer_hash.update(os.environ.get("NIKA_HEALTHY_CPUSET", "unaligned").encode())

    def code_fingerprint():
        return hashlib.sha256(
            (original_fingerprint() + observer_hash.hexdigest()).encode()
        ).hexdigest()[:16]

    @functools.wraps(original_fc)
    def sync_firecracker(uds, *args, **kwargs):
        original_fc(uds, *args, **kwargs)
        guest_exec(uds, installation_command())

    @functools.wraps(original_incus)
    def sync_incus(name, *args, **kwargs):
        original_incus(name, *args, **kwargs)
        guest(name, installation_command())

    image.sync_code = sync_firecracker
    golden.sync_code = sync_incus
    image.code_fingerprint = code_fingerprint
    golden.code_fingerprint = code_fingerprint
