"""Bulk static download published by every web-serving scenario at baseline.

A release image served beside the site's normal pages, like a software
mirror's installer. Business workloads do not fetch it; it exists in healthy
and faulty runs alike, so its presence says nothing about an injected fault.
"""

from __future__ import annotations

DOWNLOAD_OBJECT_PATH = "downloads/release-image.iso"
DOWNLOAD_OBJECT_MB = 4


def download_object_commands(docroot: str) -> list[str]:
    """Startup commands that publish the download object under ``docroot``."""
    return [
        f"mkdir -p {docroot}/downloads",
        f"dd if=/dev/urandom of={docroot}/{DOWNLOAD_OBJECT_PATH} bs=1M "
        f"count={DOWNLOAD_OBJECT_MB} status=none 2>/dev/null || true",
    ]
