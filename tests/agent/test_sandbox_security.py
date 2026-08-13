from __future__ import annotations

import pytest
from tests.agent.sandbox_support import (
    run_security_probe_with_gateway,
    sandbox_runtime_available,
)


@pytest.mark.skipif(not sandbox_runtime_available(), reason="sbx not available")
class SandboxSecurityIntegrationTest:
    def test_security_probe_with_gateway(self) -> None:
        run_security_probe_with_gateway()
