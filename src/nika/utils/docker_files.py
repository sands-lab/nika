"""Transfer analyzer inputs through Docker's API to the daemon filesystem."""

import io
import tarfile
from pathlib import Path


def upload_paths(container, paths: dict[str, Path]) -> None:
    """Copy local files or directories to absolute paths in a stopped container."""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for destination, source in paths.items():
            tar.add(source, arcname=destination.lstrip("/"))
    container.put_archive("/", archive.getvalue())


def read_file(container, path: str) -> bytes:
    """Read one regular file from a container without extracting archive paths."""
    chunks, _ = container.get_archive(path)
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
        with archive.extractfile(Path(path).name) as source:
            return source.read()
