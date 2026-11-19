"""Containerlab dc_clos light-startup smoke."""

from __future__ import annotations

import pytest

from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import containerlab_prerequisites

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ci_smoke,
    pytest.mark.skipif(
        not containerlab_prerequisites(),
        reason="containerlab (clab) + gnmic required",
    ),
]


def test_dc_clos_light_startup() -> None:
    helper = IntegrationTestCase()
    session_id = helper._start_env("dc_clos", ["-s", "s", "--backend", "containerlab"])
    try:
        helper._assert_session_ready(session_id, "dc_clos")
    finally:
        helper._close_session(session_id)
