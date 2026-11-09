#!/usr/bin/env bash
# Download a pinned GitHub release archive and verify its release checksum.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "Usage: $0 REPOSITORY TAG ARCHIVE OUTPUT" >&2
  exit 2
fi
repo="$1" tag="$2" archive="$3" output="$4"
base="https://github.com/${repo}/releases/download/${tag}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for asset in "$archive" checksums.txt; do
  if curl -fsSL --retry 4 --retry-max-time 120 "${base}/${asset}" -o "${tmp}/${asset}"; then
    continue
  fi
  # The release download edge can fail while the official assets API works.
  if [[ ! -f "${tmp}/release.json" ]]; then
    curl -fsSL --retry 4 --retry-max-time 120 \
      "https://api.github.com/repos/${repo}/releases/tags/${tag}" -o "${tmp}/release.json"
  fi
  asset_url="$(python3 - "${tmp}/release.json" "$asset" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    assets = json.load(handle)["assets"]
print(next(asset["url"] for asset in assets if asset["name"] == sys.argv[2]))
PY
)"
  curl -fsSL --retry 4 --retry-max-time 120 -H 'Accept: application/octet-stream' \
    "$asset_url" -o "${tmp}/${asset}"
done
(cd "$tmp" && awk -v file="$archive" '$2 == file' checksums.txt | sha256sum -c --quiet -)
mv "${tmp}/${archive}" "$output"
