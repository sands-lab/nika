"""Size-agnostic L3 Clos topology model for sdn_l3_clos."""

from __future__ import annotations

from dataclasses import dataclass
from nika.net_env.clos.design import (  # noqa: F401
    SIZE_TABLE,
    POD,
    VIRTUAL_ROUTER_MAC,
    TopoSize,
    EndpointInfo,
    ClosFabricModel as ClosDesign,
    build_clos_fabric_model as build_design,
    fabric_dimensions,
    rack_prefix,
    gateway_ip,
    endpoint_ip,
    mac_for_endpoint,
    mac_for_switch_port,
)
from nika.net_env.clos.design import SwitchPort  # noqa: F401
from nika.net_env.utils.kathara.docker_files.docker_images import (
    KATHARA_SDN_IMAGE,
    nika_image,
)


# Lab image is nika/onos (see Dockerfile.onos); built on deploy via
# ensure_nika_docker_images for the host architecture.
ONOS_IMAGE = nika_image("onos")
SWITCH_IMAGE = KATHARA_SDN_IMAGE
BASE_IMAGE = nika_image("base")
NGINX_IMAGE = nika_image("nginx")

OOB_NETWORK = "172.31.0.0/16"
ONOS_OOB_IP = "172.31.0.100"
FABRIC_MGR_OOB_IP = "172.31.0.101"
ONOS_OF_PORT = 6653
ONOS_REST_PORT = 8181


def dpid_for_leaf(leaf_id: int) -> str:
    """Return 16-hex DPID string for leaf (1-based)."""
    return f"{0x1000 + leaf_id:016x}"


def dpid_for_spine(spine_id: int) -> str:
    return f"{0x2000 + spine_id:016x}"


def device_id(dpid_hex: str) -> str:
    return f"of:{dpid_hex}"


@dataclass
class ClosFabricModel(ClosDesign):
    def expected_device_ids(self) -> list[str]:
        ids = [device_id(dpid_for_spine(i)) for i in range(1, self.spine_count + 1)]
        ids.extend(device_id(dpid_for_leaf(i)) for i in range(1, self.leaf_count + 1))
        return ids


def build_clos_fabric_model(
    topo_size: TopoSize, *, design: ClosDesign | None = None
) -> ClosFabricModel:
    return ClosFabricModel(**vars(design or build_design(topo_size)))
