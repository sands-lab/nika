"""Kathara K8s API (re-exported from shared lab service)."""

from nika.service.kathara.base_api import KatharaBaseAPI
from nika.service.lab.k8s_api import K8sAPIMixin

__all__ = ["K8sAPIMixin", "KatharaK8sAPI"]


class KatharaK8sAPI(KatharaBaseAPI, K8sAPIMixin):
    pass
