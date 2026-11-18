#!/usr/bin/env bash
# Regenerate the frozen agent sandbox requirements from uv.lock.
#
# Each src/nika/agents/sandbox/requirements/<set>.in lists the top-level
# packages of one agent environment; <set>.txt pins them and their
# dependencies at the versions of uv.lock, for the sandbox's Python 3.14.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dir="$root/src/nika/agents/sandbox/requirements"
constraints="$(mktemp)"
trap 'rm -f "$constraints"' EXIT

uv export --frozen --all-extras --no-hashes --no-emit-project --no-dev \
    --prerelease=allow --project "$root" \
    | grep -E '^[A-Za-z0-9]' | sed 's/ *\\$//' >"$constraints"

for input in "$dir"/*.in; do
    name="$(basename "$input" .in)"
    pins="$(uv pip compile "$input" -c "$constraints" --python-version 3.14 \
        --python-platform x86_64-manylinux_2_17 --prerelease=allow \
        --no-header --no-annotate --quiet)"
    {
        echo "# Frozen packages of the $name agent environment (Python 3.14, Linux)."
        echo "# Top-level: $(paste -sd, "$input" | sed 's/,/, /g')."
        echo "# Regenerate with scripts/compile_agent_requirements.sh."
        echo
        echo "$pins"
    } >"$dir/$name.txt"
done
