"""Two-endpoint small fabrics used by benchmark release 0.3.0."""

from nika.net_env.clos.design import build_clos_fabric_model, fabric_dimensions
from nika.net_env.sdn_l3_clos.l3_clos_topo import SDNL3Clos as CurrentSDN
from nika.net_env.sdn_l3_clos.topology_model import build_clos_fabric_model as sdn_model
from nika.net_env.p4_dc_fabric.lab import P4DcFabric as CurrentP4
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model as p4_model


def published_design(size):
    dimensions = (2, 4, 2) if size == "s" else fabric_dimensions(size)
    return build_clos_fabric_model(size, dimensions=dimensions)


class SDNL3Clos(CurrentSDN):
    L2_CAPABILITIES = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.l2_endpoint_groups

    @staticmethod
    def build_model(size):
        return sdn_model(size, design=published_design(size))


class P4DcFabric(CurrentP4):
    L2_CAPABILITIES = None

    @staticmethod
    def build_model(size):
        return p4_model(size, design=published_design(size))
