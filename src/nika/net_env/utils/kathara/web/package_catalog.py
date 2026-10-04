"""Build the reproducible software-catalog fixture before the repository starts."""

import gzip
import hashlib
from pathlib import Path


def records():
    for index in range(65536):
        record = (
            f"Package: campus-tool-{index}\nVersion: 1.0\nArchitecture: amd64\n"
            f"Filename: packages/campus-tool-{index}.deb\nSize: 16777216\n"
            f"SHA256: {hashlib.sha256(str(index).encode()).hexdigest()}\n"
            "Description: Tools for teaching, numerical analysis and data processing.\n"
        )
        description = " Documentation and examples for the campus laboratory.\n"
        body = record + description * ((1024 - len(record)) // len(description))
        yield (body[:1022].ljust(1022) + "\n\n").encode("ascii")


if __name__ == "__main__":
    root = Path("/var/www/packages")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "catalog.txt").open("wb") as raw:
        for record in records():
            raw.write(record)
    with (root / "catalog.txt.gz").open("wb") as compressed:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=compressed, mtime=0
        ) as stream:
            for record in records():
                stream.write(record)
