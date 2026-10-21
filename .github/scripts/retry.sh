#!/usr/bin/env bash
# Run a command, retrying it up to RETRIES times (default 3) after the first
# failure with a linear backoff. Exits with the last failure status.
set -uo pipefail

retries="${RETRIES:-3}"
delay="${RETRY_DELAY:-10}"

for ((attempt = 1; ; attempt++)); do
  "$@" && exit 0
  status=$?
  if ((attempt > retries)); then
    echo "::error::Failed after ${attempt} attempts (exit ${status}): $*" >&2
    exit "${status}"
  fi
  echo "::warning::Attempt ${attempt} failed (exit ${status}); retrying in $((delay * attempt))s: $*" >&2
  sleep "$((delay * attempt))"
done
