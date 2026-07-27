from nika.service.kathara.base_api import KatharaBaseAPI
from nika.service.kathara.bmv2_api import BMv2APIMixin, KatharaBMv2API
from nika.service.kathara.frr_api import FRRAPIMixin, KatharaFRRAPI
from nika.service.kathara.intf_api import IntfAPIMixin, KatharaIntfAPI
from nika.service.kathara.k8s_api import KatharaK8sAPI, K8sAPIMixin
from nika.service.kathara.nftable_api import KatharaNFTableAPI, NFTableMixin
from nika.service.kathara.tc_api import KatharaTCAPI, TCMixin
from nika.service.kathara.telemetry_api import KatharaTelemetryAPI, TelemetryAPIMixin

__all__ = [
    "KatharaAPIALL",
    "KatharaBaseAPI",
    "KatharaBMv2API",
    "KatharaFRRAPI",
    "KatharaIntfAPI",
    "KatharaNFTableAPI",
    "KatharaTCAPI",
    "KatharaTelemetryAPI",
    "KatharaK8sAPI",
]


class KatharaAPIALL(
    KatharaBaseAPI,
    BMv2APIMixin,
    FRRAPIMixin,
    IntfAPIMixin,
    NFTableMixin,
    TCMixin,
    TelemetryAPIMixin,
    K8sAPIMixin
):
    """
    Combined API for all Kathara functionalities.
    """

    pass
