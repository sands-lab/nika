"""Size-agnostic L3 Clos topology model for p4_dc_fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
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
from nika.net_env.utils.kathara.docker_files.docker_images import (
    KATHARA_P4_IMAGE,
    nika_image,
)


SWITCH_IMAGE = KATHARA_P4_IMAGE
BASE_IMAGE = nika_image("base")
NGINX_IMAGE = nika_image("nginx")
FABRIC_CONTROLLER_IMAGE = nika_image("fabric-controller")

OOB_NETWORK = "172.31.0.0/16"
FABRIC_MGR_OOB_IP = "172.31.0.101"
P4RUNTIME_PORT = 9559
PIPELINE_NAME = "fabric"


IPV4_LPM_SIZE = 256
ECMP_MEMBER_SIZE = 256
ECMP_GROUP_SIZE = 128
PORT_COUNTER_SIZE = 32


def bmv2_port_for_eth(eth_index: int) -> int:
    """BMv2 dataplane port id for data-plane ethN (1-based, OOB excluded)."""
    return eth_index + 1


def leaf_device_id(leaf_id: int) -> int:
    return leaf_id


def spine_device_id(spine_id: int) -> int:
    return 100 + spine_id


def leaf_oob_ip(leaf_id: int) -> str:
    return f"172.31.0.{10 + leaf_id}"


def spine_oob_ip(spine_id: int) -> str:
    return f"172.31.0.{80 + spine_id}"


@dataclass(frozen=True)
class SwitchPort:
    name: str  # ethN
    peer: str
    mac: str
    role: Literal["host", "spine", "leaf"]
    bmv2_port: int


@dataclass(frozen=True)
class SwitchInfo:
    name: str
    kind: Literal["spine", "leaf"]
    switch_id: int
    device_id: int
    oob_ip: str


@dataclass
class ClosFabricModel(ClosDesign):
    switch_info: dict[str, SwitchInfo] = field(default_factory=dict)

    def expected_device_ids(self) -> list[int]:
        return [self.switch_info[name].device_id for name in self.spines + self.leaves]

    def expected_leaf_spine_link_count(self) -> int:
        return self.spine_count * self.leaf_count

    def fabric_switches(self) -> list[str]:
        return list(self.spines) + list(self.leaves)


def build_clos_fabric_model(
    topo_size: TopoSize, *, design: ClosDesign | None = None
) -> ClosFabricModel:
    design = design or build_design(topo_size)
    model = ClosFabricModel(**vars(design))
    for leaf_id, leaf in enumerate(model.leaves, start=1):
        model.switch_info[leaf] = SwitchInfo(
            name=leaf,
            kind="leaf",
            switch_id=leaf_id,
            device_id=leaf_device_id(leaf_id),
            oob_ip=leaf_oob_ip(leaf_id),
        )
    for spine_id, spine in enumerate(model.spines, start=1):
        model.switch_info[spine] = SwitchInfo(
            name=spine,
            kind="spine",
            switch_id=spine_id,
            device_id=spine_device_id(spine_id),
            oob_ip=spine_oob_ip(spine_id),
        )

    model.ports = {
        name: [
            SwitchPort(**vars(port), bmv2_port=bmv2_port_for_eth(i))
            for i, port in enumerate(ports)
        ]
        for name, ports in design.ports.items()
    }
    return model
