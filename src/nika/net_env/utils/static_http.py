"""Stage a static HTTP service with byte-range support."""

from typing import Any

from nika.config import pkg_path


def static_http_commands(machine: Any, *, docroot: str, port: int = 80) -> list[str]:
    """Install the service and return its endpoint startup commands."""
    machine.create_file_from_path(
        str(pkg_path("net_env/utils/kathara/static_httpd.py")),
        "/usr/local/bin/static-httpd.py",
    )
    return [
        f"mkdir -p {docroot}",
        f"test -s {docroot}/small.bin || dd if=/dev/zero of={docroot}/small.bin "
        "bs=1024 count=16 status=none",
        f"test -s {docroot}/large.bin || dd if=/dev/zero of={docroot}/large.bin "
        "bs=1M count=16 status=none",
        f"nohup python3 /usr/local/bin/static-httpd.py {port} {docroot} "
        "</dev/null >/dev/null 2>&1 &",
    ]
