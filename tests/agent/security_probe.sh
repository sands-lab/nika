#!/usr/bin/env bash
# Security checks for the NIKA agent sandbox container (used by tests).
set -euo pipefail

failures=0

check() {
    local name="$1"
    shift
    if "$@"; then
        echo "PASS: $name"
    else
        echo "FAIL: $name"
        failures=$((failures + 1))
    fi
}

# Docker socket / CLI checks depend on sandbox type.
# Native sbx microVMs include an isolated Docker daemon (not the host daemon).
if [ -z "${NIKA_SBX_NATIVE:-}" ]; then
    check "no_docker_socket" test ! -S /var/run/docker.sock
    check "no_docker_cli" test -z "$(command -v docker || true)"
else
    echo "PASS: sbx_isolated_docker_expected"
fi

# Host home must not be mounted (agent user home is /home/agent only).
check "no_host_home_mount" test ! -d /root/.ssh

# Unauthorized host probe file must not be readable.
if [ -n "${NIKA_SANDBOX_PROBE_FILE:-}" ]; then
    check "no_host_probe_file" test ! -r "$NIKA_SANDBOX_PROBE_FILE"
fi

# Ground truth must not be readable from inside the sandbox.
if [ -n "${NIKA_SANDBOX_GROUND_TRUTH:-}" ]; then
    check "no_ground_truth_file" test ! -r "$NIKA_SANDBOX_GROUND_TRUTH"
fi
check "no_ground_truth_in_workspace" test ! -r ./ground_truth.json

# Sandbox run.json must not expose injected failure labels.
if [ -f ./run.json ]; then
    if python3 -c '
import json, sys
meta = json.load(open("run.json", encoding="utf-8"))
sys.exit(1 if ("problem_names" in meta or "failure_domain" in meta) else 0)
'; then
        echo "PASS: run_json_no_failure_labels"
    else
        echo "FAIL: run_json_no_failure_labels"
        failures=$((failures + 1))
    fi
fi

# If gateway health check fails, print URL for debugging.
if [ -n "${NIKA_MCP_GATEWAY_AGENT_URL:-}" ]; then
    health_url="${NIKA_MCP_GATEWAY_AGENT_URL%/}/gateway/health"
    if curl -sf "$health_url" >/dev/null; then
        echo "PASS: gateway_health"
    else
        echo "FAIL: gateway_health ($health_url)"
        failures=$((failures + 1))
    fi
fi

# Docker CLI must not be available on the host-style sandbox.
if [ -z "${NIKA_SBX_NATIVE:-}" ]; then
    check "no_docker_cli" test -z "$(command -v docker || true)"
fi

# Kathara CLI must not be available inside sandbox.
check "no_kathara_cli" test -z "$(command -v kathara || true)"

# Secrets must be redacted in env dump artifact when present.
if [ -f /workspace/env_dump.txt ]; then
    if grep -qE 'sk-[A-Za-z0-9_-]{10,}' /workspace/env_dump.txt; then
        echo "FAIL: env_dump_contains_secrets"
        failures=$((failures + 1))
    else
        echo "PASS: env_dump_no_raw_secrets"
    fi
fi

if [ "$failures" -gt 0 ]; then
    echo "Security probe failures: $failures"
    exit 1
fi

echo "All security probes passed"
