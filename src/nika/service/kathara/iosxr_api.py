"""Kathara IOS-XR API (re-exported from shared lab service)."""

from nika.service.kathara.base_api import KatharaBaseAPI
from nika.service.lab.iosxr_api import IOSXRAPIMixin

__all__ = ["IOSXRAPIMixin", "KatharaIOSXRAPI"]


class KatharaIOSXRAPI(KatharaBaseAPI, IOSXRAPIMixin):
    pass
