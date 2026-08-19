"""Production-style centralized L3 Clos SDN fabric (ONOS + OVS)."""

from __future__ import annotations

import textwrap
from ipaddress import IPv4Network
from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.kathara.sdn.topology_model import (
    BASE_IMAGE,
    FABRIC_MGR_OOB_IP,
    NGINX_IMAGE,
    ONOS_IMAGE,
    ONOS_OF_PORT,
    ONOS_OOB_IP,
    SWITCH_IMAGE,
    VIRTUAL_ROUTER_MAC,
    ClosFabricModel,
    TopoSize,
    build_clos_fabric_model,
    dpid_for_leaf,
    dpid_for_spine,
    gateway_ip,
)

# Host kernels advertise many CPUs while switch containers are ~512MiB; default
# ovs-vswitchd sizing (~1.25×ncpu upcall threads) aborts and ovs-vsctl hangs.
_OVS_HANDLER_THREADS = 2
_OVS_REVALIDATOR_THREADS = 1


def _ovs_start_commands() -> list[str]:
    return [
        "mkdir -p /etc/openvswitch /var/run/openvswitch /var/log/openvswitch",
        "ovsdb-tool create /etc/openvswitch/conf.db "
        "/usr/share/openvswitch/vswitch.ovsschema 2>/dev/null || true",
        "ovsdb-server --remote=punix:/var/run/openvswitch/db.sock "
        "--pidfile=/var/run/openvswitch/ovsdb-server.pid "
        "--detach --log-file=/var/log/openvswitch/ovsdb-server.log",
        "ovs-vsctl --no-wait --timeout=10 init",
        "ovs-vsctl --no-wait --timeout=10 set Open_vSwitch . "
        f"other_config:n-handler-threads={_OVS_HANDLER_THREADS} "
        f"other_config:n-revalidator-threads={_OVS_REVALIDATOR_THREADS}",
        "ovs-vswitchd --pidfile=/var/run/openvswitch/ovs-vswitchd.pid "
        "--detach --log-file=/var/log/openvswitch/ovs-vswitchd.log",
    ]


class SwitchMeta:
    def __init__(
        self, name: str, machine: Machine, eth_index: int, cmd_list: list[str]
    ):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list


class HostMeta:
    def __init__(
        self, name: str, machine: Machine, eth_index: int, cmd_list: list[str]
    ):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.ip_address: str | None = None


class SDNL3Clos(NetworkEnvBase):
    LAB_NAME = "sdn_l3_clos"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["link", "sdn", "pc", "mac", "arp", "icmp", "http"]
    # ONOS JVM + OF sessions + proactive reconcile need more than the default 180s.
    VERIFY_MAX_WAIT_SEC = 420

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.topo_size: TopoSize = topo_size
        self.model: ClosFabricModel = build_clos_fabric_model(topo_size)
        self.web_urls = list(self.model.web_urls)
        self._host_ips = {e.name: e.ip for e in self.model.endpoints}

        self.desc = textwrap.dedent(f"""\
            Symmetric leaf-spine L3 Clos SDN fabric under centralized ONOS control.
            Size {topo_size}: {self.model.spine_count} spines, {self.model.leaf_count} leaves,
            {self.model.endpoints_per_leaf} endpoints per leaf.
            Per-leaf rack prefix 10.0.<leaf>.0/24 with virtual gateway MAC {VIRTUAL_ROUTER_MAC}.
            OpenFlow 1.3 SELECT ECMP across spines; OOB control network 172.31.0.0/16;
            OVS fail-mode=secure; no STP and no NORMAL fallback.""")

        spine_metas: list[SwitchMeta] = []
        leaf_metas: list[SwitchMeta] = []

        for spine in self.model.spines:
            machine = self.lab.new_machine(
                spine, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            spine_metas.append(
                SwitchMeta(name=spine, machine=machine, eth_index=0, cmd_list=[])
            )

        for leaf in self.model.leaves:
            machine = self.lab.new_machine(
                leaf, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            leaf_metas.append(
                SwitchMeta(name=leaf, machine=machine, eth_index=0, cmd_list=[])
            )

        # Endpoints
        host_metas: list[HostMeta] = []
        for ep in self.model.endpoints:
            image = NGINX_IMAGE if ep.role == "web" else BASE_IMAGE
            machine = self.lab.new_machine(
                ep.name, **{"image": image, "cpus": 0.5, "mem": "256m"}
            )
            host_metas.append(
                HostMeta(name=ep.name, machine=machine, eth_index=0, cmd_list=[])
            )
        host_by_name = {h.name: h for h in host_metas}

        # ONOS + fabric manager on OOB
        onos = self.lab.new_machine(
            "onos",
            **{
                "image": ONOS_IMAGE,
                "cpus": 2,
                "mem": "2048m",
                "bridged": True,
            },
        )
        onos.add_meta(
            "env", "ONOS_APPS=drivers,openflow-base,lldpprovider,hostprovider"
        )
        onos.add_meta("env", "JAVA_OPTS=-Xmx1536m")
        fabric_mgr = self.lab.new_machine(
            "fabric_mgr",
            **{"image": BASE_IMAGE, "cpus": 0.5, "mem": "256m"},
        )

        for meta in spine_metas + leaf_metas:
            meta.cmd_list.extend(_ovs_start_commands())
            meta.cmd_list.append(f"ovs-vsctl add-br {meta.name}")
            meta.cmd_list.append(f"ovs-vsctl set-fail-mode {meta.name} secure")
            meta.cmd_list.append(
                f"ovs-vsctl set bridge {meta.name} protocols=OpenFlow13"
            )

        # Host ↔ leaf attachments (eth order must match topology_model)
        for leaf_id, leaf_meta in enumerate(leaf_metas, start=1):
            for ep in self.model.endpoints_on_leaf(leaf_id):
                host_meta = host_by_name[ep.name]
                link_name = f"{ep.name}_{leaf_meta.name}"
                self.lab.connect_machine_to_link(host_meta.machine.name, link_name)
                self.lab.connect_machine_to_link(leaf_meta.machine.name, link_name)

                port = self.model.port_to_peer(leaf_meta.name, ep.name)
                assert port is not None
                gw = gateway_ip(leaf_id)

                host_meta.ip_address = ep.ip
                host_meta.cmd_list.extend(
                    [
                        f"ip link set eth{host_meta.eth_index} address {ep.mac}",
                        f"ip addr add {ep.ip}/24 dev eth{host_meta.eth_index}",
                        f"ip link set eth{host_meta.eth_index} up",
                        f"ip route replace default via {gw}",
                        f"ip neigh replace {gw} lladdr {VIRTUAL_ROUTER_MAC} "
                        f"dev eth{host_meta.eth_index} nud permanent",
                    ]
                )
                if ep.role == "web":
                    host_meta.cmd_list.extend(
                        [
                            "mkdir -p /var/www/html",
                            f"echo 'sdn_l3_clos web_{leaf_id}' > /var/www/html/index.html",
                            "nginx || service nginx start || true",
                        ]
                    )
                host_meta.eth_index += 1

                leaf_meta.cmd_list.append(
                    f"ip link set eth{leaf_meta.eth_index} address {port.mac}"
                )
                leaf_meta.cmd_list.append(
                    f"ovs-vsctl add-port {leaf_meta.name} eth{leaf_meta.eth_index}"
                )
                leaf_meta.cmd_list.append(f"ip link set eth{leaf_meta.eth_index} up")
                leaf_meta.eth_index += 1

        # Leaf ↔ spine full mesh
        for leaf_meta in leaf_metas:
            for spine_meta in spine_metas:
                link_name = f"{spine_meta.name}_{leaf_meta.name}"
                self.lab.connect_machine_to_link(spine_meta.machine.name, link_name)
                self.lab.connect_machine_to_link(leaf_meta.machine.name, link_name)

                leaf_port = self.model.port_to_peer(leaf_meta.name, spine_meta.name)
                spine_port = self.model.port_to_peer(spine_meta.name, leaf_meta.name)
                assert leaf_port is not None and spine_port is not None

                leaf_meta.cmd_list.append(
                    f"ip link set eth{leaf_meta.eth_index} address {leaf_port.mac}"
                )
                leaf_meta.cmd_list.append(
                    f"ovs-vsctl add-port {leaf_meta.name} eth{leaf_meta.eth_index}"
                )
                leaf_meta.cmd_list.append(f"ip link set eth{leaf_meta.eth_index} up")
                leaf_meta.eth_index += 1

                spine_meta.cmd_list.append(
                    f"ip link set eth{spine_meta.eth_index} address {spine_port.mac}"
                )
                spine_meta.cmd_list.append(
                    f"ovs-vsctl add-port {spine_meta.name} eth{spine_meta.eth_index}"
                )
                spine_meta.cmd_list.append(f"ip link set eth{spine_meta.eth_index} up")
                spine_meta.eth_index += 1

        # OOB control network 172.31.0.0/16
        oob_hosts = IPv4Network("172.31.0.0/16").hosts()
        # Reserve .100 / .101
        reserved = {ONOS_OOB_IP, FABRIC_MGR_OOB_IP}
        for meta in spine_metas + leaf_metas:
            while True:
                switch_ip = str(next(oob_hosts))
                if switch_ip not in reserved:
                    break
            self.lab.connect_machine_to_link(meta.machine.name, "oob_control")
            # OOB NIC is NOT added to the OVS bridge
            meta.cmd_list.append(f"ip addr add {switch_ip}/16 dev eth{meta.eth_index}")
            meta.cmd_list.append(f"ip link set eth{meta.eth_index} up")
            dpid = (
                dpid_for_spine(int(meta.name.split("_")[1]))
                if meta.name.startswith("spine_")
                else dpid_for_leaf(int(meta.name.split("_")[1]))
            )
            meta.cmd_list.append(
                f"ovs-vsctl set bridge {meta.name} other-config:datapath-id={dpid}"
            )
            meta.cmd_list.append(
                f"ovs-vsctl set-controller {meta.name} tcp:{ONOS_OOB_IP}:{ONOS_OF_PORT}"
            )
            meta.eth_index += 1

        self.lab.connect_machine_to_link(onos.name, "oob_control")
        self.lab.connect_machine_to_link(fabric_mgr.name, "oob_control")

        self.lab.create_file_from_list(
            [
                f"ip addr add {ONOS_OOB_IP}/16 dev eth0",
                "ip link set eth0 up",
                # Entry point already starts onos-service; ensure OOB is up first.
                "sleep 1",
            ],
            "onos.startup",
        )

        self.lab.create_file_from_list(
            [
                f"ip addr add {FABRIC_MGR_OOB_IP}/16 dev eth0",
                "ip link set eth0 up",
            ],
            "fabric_mgr.startup",
        )

        for meta in spine_metas + leaf_metas:
            self.lab.create_file_from_list(
                meta.cmd_list, f"{meta.machine.name}.startup"
            )
        for host_meta in host_metas:
            self.lab.create_file_from_list(
                host_meta.cmd_list, f"{host_meta.machine.name}.startup"
            )

        self.load_machines()
        # Classify web servers for inject_resolve
        for ep in self.model.web_endpoints():
            if ep.name not in self.servers.get("web", []):
                self.servers.setdefault("web", []).append(ep.name)
        self.servers["web"] = sorted(set(self.servers.get("web", [])))

    def deploy(self):
        """Start containers, then program L3/ECMP OpenFlow rules on the fabric."""
        super().deploy()
        from nika.net_env.kathara.sdn.fabric_manager import reconcile_fabric

        reconcile_fabric(self._build_runtime(), self.model, wait_onos=True)

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.sdn.verify import verify_sdn_l3_clos_lab

        return verify_sdn_l3_clos_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            model=self.model,
        )
