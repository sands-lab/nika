"""Real arm64 NIKA user path for sdn_l3_clos via QEMU (test-only).

Runs ensure + live Clos E2E inside an arm64 container that talks to the host
Docker engine with DOCKER_DEFAULT_PLATFORM=linux/arm64, so Kathara sees arm64
the same way a Linux arm64 user would.

Full dataplane deploy can hit a Kathara network-plugin limitation on amd64 hosts
(``failed to set tap speed ... inappropriate ioctl``) when starting arm64
containers. That is orthogonal to the ONOS multi-arch image fix; the architecture
gate and image builds are still asserted hard.
"""

from __future__ import annotations

import re

import pytest

from tests.support.arm64_user_e2e import (
    arm64_user_e2e_available,
    restore_host_arch_nika_onos,
    run_in_arm64_nika_user,
)
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
    pytest.mark.skipif(
        not arm64_user_e2e_available(),
        reason="qemu-aarch64 binfmt unavailable (need privileged docker binfmt)",
    ),
]

_KATHARA_TAP_IOCTL = "failed to set tap speed"
_ARCH_GATE = "InvalidImageArchitectureError"


def test_sdn_l3_clos_arm64_user_path_e2e() -> None:
    """Build arm64 Clos images and exercise the real arm64 NIKA/Kathara path."""
    from tests.support.arm64_user_e2e import ensure_arm64_user_runner_image

    if not ensure_arm64_user_runner_image():
        pytest.skip("failed to build local arm64 user runner image")

    script = r"""
set -euo pipefail
test "$(uname -m)" = "aarch64"
docker buildx version >/dev/null

for attempt in 1 2 3 4 5; do
  if uv sync --extra labs --group dev; then
    break
  fi
  echo "uv sync failed (attempt ${attempt}); retrying..."
  sleep $((attempt * 5))
  if [ "${attempt}" -eq 5 ]; then
    exit 1
  fi
done

DOCKER_FILES=src/nika/net_env/utils/kathara/docker_files
# docker-py builds ignore DOCKER_DEFAULT_PLATFORM; build non-ONOS images via CLI.
docker rmi nika/onos nika/nginx nika/base >/dev/null 2>&1 || true
docker build --platform linux/arm64 --network=host \
  -t nika/base -f "${DOCKER_FILES}/Dockerfile.base" "${DOCKER_FILES}"
docker build --platform linux/arm64 --network=host \
  -t nika/nginx -f "${DOCKER_FILES}/Dockerfile.nginx" "${DOCKER_FILES}"
# ONOS uses BuildKit multi-stage (nika's ensure path).
uv run python -m nika.net_env.utils.kathara.docker_files.docker_images -f nika/onos

for img in nika/base nika/nginx nika/onos; do
  a="$(docker image inspect "$img" --format '{{.Architecture}}')"
  echo "${img} ARCH=${a}"
  test "${a}" = "arm64"
done
echo IMAGES_OK

set +e
uv run pytest tests/nika/net_env/test_sdn_l3_clos.py::SDNL3ClosTopologyChangeTest -v --tb=short
clos_rc=$?
set -e
echo "CLOS_RC=${clos_rc}"
exit "${clos_rc}"
"""
    try:
        result = run_in_arm64_nika_user(script, timeout=7200.0)
        output = (result.stdout or "") + "\n" + (result.stderr or "")

        assert "IMAGES_OK" in output, output[-8000:]
        assert re.search(r"nika/onos ARCH=arm64", output), output[-4000:]
        assert _ARCH_GATE not in output, (
            "Kathara rejected foreign-arch images; arm64 user path is broken:\n"
            + output[-8000:]
        )

        if result.returncode == 0:
            assert "PASSED" in output, output[-4000:]
            return

        if _KATHARA_TAP_IOCTL in output:
            pytest.xfail(
                "Kathara network plugin cannot start arm64 containers on this "
                "amd64 host (tap speed ioctl). Image build + arch gate passed."
            )

        assert False, (
            f"arm64 user E2E failed (rc={result.returncode}):\n{output[-12000:]}"
        )
    finally:
        restore_host_arch_nika_onos()
