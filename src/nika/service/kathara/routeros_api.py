"""Kathara MikroTik RouterOS API (re-exported from shared lab service)."""

from nika.service.kathara.base_api import KatharaBaseAPI
from nika.service.lab.routeros_api import RouterOSAPIMixin

__all__ = ["KatharaRouterOSAPI", "RouterOSAPIMixin"]


class KatharaRouterOSAPI(KatharaBaseAPI, RouterOSAPIMixin):
    pass
