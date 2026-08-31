"""Parametrized Kathara API smoke across representative scenarios."""

from __future__ import annotations

from typing import ClassVar

import pytest

from nika.net_env.campus_lan.verify import PROBE_HOST as CAMPUS_PROBE
from tests.support.kathara_api_base import KatharaScenarioApiSmokeTest
from tests.support.prerequisites import docker_available


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaSimpleBgpApiSmoke(KatharaScenarioApiSmokeTest):
    SCENARIO = "simple_bgp"
    PROBE_HOST = "pc1"


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaCampusLanApiSmoke(KatharaScenarioApiSmokeTest):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s"]
    PROBE_HOST = CAMPUS_PROBE
