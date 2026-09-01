from __future__ import annotations

from typing import ClassVar

from nika.runtime.factory import runtime_for_session
from nika.service.kathara import KatharaBaseAPI
from nika.service.kathara.frr_api import KatharaFRRAPI
from nika.service.kathara.intf_api import KatharaIntfAPI
from nika.service.kathara.nftable_api import KatharaNFTableAPI
from nika.service.kathara.tc_api import KatharaTCAPI
from tests.support.api_smoke import ApiSmokeMixin
from tests.support.integration_base import SharedSessionTestCase


class KatharaScenarioApiSmokeTest(SharedSessionTestCase, ApiSmokeMixin):
    """One shared lab per class; subclasses set ``SCENARIO``, ``PROBE_HOST``, optional ``ENV_RUN_ARGS``."""

    __test__ = False
    ENV_RUN_ARGS: ClassVar[list[str]] = []
    PROBE_HOST: ClassVar[str] = "pc1"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls is not KatharaScenarioApiSmokeTest:
            cls.__test__ = True

    def _lab_name(self) -> str:
        return str(self._session_row(self.session_id)["lab_name"])

    def _runtime(self):
        return runtime_for_session(self._session_row(self.session_id))

    def _host_api(self) -> KatharaBaseAPI:
        return KatharaBaseAPI(lab_name=self._lab_name())

    def _frr_api(self) -> KatharaFRRAPI:
        return KatharaFRRAPI(lab_name=self._lab_name())

    def _intf_api(self) -> KatharaIntfAPI:
        return KatharaIntfAPI(lab_name=self._lab_name())

    def _tc_api(self) -> KatharaTCAPI:
        return KatharaTCAPI(lab_name=self._lab_name())

    def _nft_api(self) -> KatharaNFTableAPI:
        return KatharaNFTableAPI(lab_name=self._lab_name())

    def test_runtime_list_nodes_and_exec(self) -> None:
        runtime = self._runtime()
        nodes = runtime.list_nodes()
        assert nodes
        assert self.PROBE_HOST in nodes
        out = runtime.exec(self.PROBE_HOST, "hostname", timeout=15)
        assert out.strip()

    def test_host_api_reachability(self) -> None:
        api = self._host_api()
        cfg = api.get_host_net_config(self.PROBE_HOST)
        assert cfg
