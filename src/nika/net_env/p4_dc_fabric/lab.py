"""P4Runtime-controlled L3 Clos fabric (Kathara + BMv2 simple_switch_grpc)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.p4_dc_fabric.fabric_manager.apply import MANAGER, MANAGER_DIR
from nika.net_env.p4_dc_fabric.topology_model import (
    BASE_IMAGE,
    FABRIC_CONTROLLER_IMAGE,
    FABRIC_MGR_OOB_IP,
    NGINX_IMAGE,
    SWITCH_IMAGE,
    VIRTUAL_ROUTER_MAC,
    ClosFabricModel,
    TopoSize,
    build_clos_fabric_model,
    gateway_ip,
)
from nika.runtime.spec import NodeRole
from nika.net_env.utils.rack_workload import (
    FANOUT_PORT,
    bulk_downloads,
    client_processes,
    fanout_processes,
    web_content_commands,
)
from nika.traffic.workload import FAN_IN_BURSTS, BusinessWorkload, WorkloadExercise

_MANAGER = Path(__file__).resolve().parents[1] / "utils" / "kathara" / "p4rt_manager.py"
# BMv2 paths carry about 5 Mbit/s of TCP, so a 2 MB object takes a few seconds.
LARGE_OBJECT_MB = 2
# Search front end and its shards. Each shard answers a query with a block of
# datagrams sent back to back; BMv2 forwards such a short block far faster than
# its sustained rate. The blocks from four shards converge on the front end's
# leaf port at once and a normal port buffer absorbs them; a 16 KB queue
# draining at 20 Mbit/s (5.7 ms) cannot.
QUERY_LEAF = 2
QUERY_SHARDS = 4
SHARD_ANSWER_DATAGRAMS = 24
SHARD_DATAGRAM_BYTES = 1400


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


def _switch_startup(
    ports: list, device_id: int, oob_eth: int, oob_ip: str
) -> list[str]:
    cmds = [
        "sysctl -w net.ipv6.conf.all.disable_ipv6=1 || true",
        "sysctl -w net.ipv4.conf.all.arp_ignore=8",
        "sysctl -w net.ipv4.conf.default.arp_ignore=8",
    ]
    port_args: list[str] = []
    for port in ports:
        eth = port.name
        cmds.append(f"ip link set {eth} address {port.mac}")
        cmds.append(f"ip link set {eth} up")
        port_args.append(f"-i {port.bmv2_port}@{eth}")
    cmds.append(f"ip addr add {oob_ip}/16 dev eth{oob_eth}")
    cmds.append(f"ip link set eth{oob_eth} up")
    iface = " ".join(port_args)
    cmds.append(
        f"simple_switch_grpc {iface} --device-id {device_id} "
        f"--no-p4 -- --grpc-server-addr 0.0.0.0:9559 >> sw.log 2>&1 &"
    )
    cmds.append(
        "for i in $(seq 1 30); do pgrep -f '[s]imple_switch_grpc' && break; sleep 1; done"
    )
    return cmds


class P4DcFabric(NetworkEnvBase):
    LAB_NAME = "p4_dc_fabric"
    TOPO_LEVEL = "medium"
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
            Symmetric leaf-spine L3 Clos programmable fabric under P4Runtime control.
            Size {topo_size}: {self.model.spine_count} spines, {self.model.leaf_count} leaves,
            {self.model.endpoints_per_leaf} endpoints per leaf.
            Per-leaf rack prefix 10.0.<leaf>.0/24 with virtual gateway MAC {VIRTUAL_ROUTER_MAC}.
            BMv2 simple_switch_grpc; OOB control network 172.31.0.0/16;
            IPv4 LPM + ActionSelector ECMP programmed from one topology model.
            Each leaf hosts one nginx web server (web_<leaf>, 10.0.<leaf>.11) and
            client hosts. Clients load pages and small objects from the web
            servers all the time and download large.bin ({LARGE_OBJECT_MB} MB)
            from one of them every few minutes. web_{QUERY_LEAF} also runs a
            search front end: about once a second it sends a query to
            the index shards on {", ".join(w.name for w in self._shards())}
            (UDP {FANOUT_PORT}), which all answer at once.""")

        spine_metas: list[SwitchMeta] = []
        leaf_metas: list[SwitchMeta] = []

        for spine in self.model.spines:
            machine = self.lab.new_machine(
                spine, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            self.declare_machine(
                spine,
                role=NodeRole.SWITCH,
                capabilities=("linux", "bmv2", "p4", "p4runtime"),
            )
            spine_metas.append(
                SwitchMeta(name=spine, machine=machine, eth_index=0, cmd_list=[])
            )
        for leaf in self.model.leaves:
            machine = self.lab.new_machine(
                leaf, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            self.declare_machine(
                leaf,
                role=NodeRole.SWITCH,
                capabilities=("linux", "bmv2", "p4", "p4runtime"),
            )
            leaf_metas.append(
                SwitchMeta(name=leaf, machine=machine, eth_index=0, cmd_list=[])
            )

        host_metas: list[HostMeta] = []
        for ep in self.model.endpoints:
            image = NGINX_IMAGE if ep.role == "web" else BASE_IMAGE
            machine = self.lab.new_machine(
                ep.name, **{"image": image, "cpus": 0.5, "mem": "256m"}
            )
            if ep.role == "web":
                self.declare_machine(
                    ep.name,
                    role=NodeRole.SERVICE,
                    capabilities=("linux", "http"),
                    service_type="web",
                    reachability_target=True,
                )
            else:
                self.declare_machine(
                    ep.name,
                    role=NodeRole.HOST,
                    capabilities=("linux",),
                    reachability_target=True,
                )
            host_metas.append(
                HostMeta(name=ep.name, machine=machine, eth_index=0, cmd_list=[])
            )
        host_by_name = {h.name: h for h in host_metas}

        fabric_mgr = self.lab.new_machine(
            "fabric_mgr",
            **{"image": FABRIC_CONTROLLER_IMAGE, "cpus": 1, "mem": "512m"},
        )
        self.declare_machine(
            fabric_mgr.name,
            role=NodeRole.CONTROLLER,
            capabilities=("linux", "p4runtime"),
        )

        for leaf_id, leaf_meta in enumerate(leaf_metas, start=1):
            for ep in self.model.endpoints_on_leaf(leaf_id):
                host_meta = host_by_name[ep.name]
                link_name = f"{ep.name}_{leaf_meta.name}"
                self.lab.connect_machine_to_link(host_meta.machine.name, link_name)
                self.lab.connect_machine_to_link(leaf_meta.machine.name, link_name)
                gw = gateway_ip(leaf_id)
                host_meta.ip_address = ep.ip
                host_meta.cmd_list.extend(
                    [
                        f"ip link set eth{host_meta.eth_index} address {ep.mac}",
                        f"ip addr add {ep.ip}/32 dev eth{host_meta.eth_index}",
                        f"ip link set eth{host_meta.eth_index} up",
                        f"ip route replace {gw} dev eth{host_meta.eth_index}",
                        f"ip route replace default via {gw}",
                        f"ip neigh replace {gw} lladdr {VIRTUAL_ROUTER_MAC} "
                        f"dev eth{host_meta.eth_index} nud permanent",
                    ]
                )
                if ep.role == "web":
                    host_meta.cmd_list.extend(
                        [
                            *web_content_commands(LARGE_OBJECT_MB),
                            f"echo 'p4_dc_fabric web_{leaf_id}' > /var/www/html/index.html",
                            "nginx || service nginx start || true",
                        ]
                    )
                host_meta.eth_index += 1
                leaf_meta.eth_index += 1

        for leaf_meta in leaf_metas:
            for spine_meta in spine_metas:
                link_name = f"{spine_meta.name}_{leaf_meta.name}"
                self.lab.connect_machine_to_link(spine_meta.machine.name, link_name)
                self.lab.connect_machine_to_link(leaf_meta.machine.name, link_name)
                leaf_meta.eth_index += 1
                spine_meta.eth_index += 1

        for meta in spine_metas + leaf_metas:
            self.lab.connect_machine_to_link(meta.machine.name, "oob_control")
        self.lab.connect_machine_to_link(fabric_mgr.name, "oob_control")

        for meta in spine_metas + leaf_metas:
            info = self.model.switch_info[meta.name]
            ports = self.model.ports[meta.name]
            oob_eth = len(ports)
            cmds = _switch_startup(ports, info.device_id, oob_eth, info.oob_ip)
            self.lab.create_file_from_list(cmds, f"{meta.machine.name}.startup")

        fabric_mgr.create_file_from_path(str(_MANAGER), "p4rt_manager.py")
        self.lab.create_file_from_list(
            [
                f"ip addr add {FABRIC_MGR_OOB_IP}/16 dev eth0",
                "ip link set eth0 up",
                f"mkdir -p /tmp/p4_fabric {MANAGER_DIR}",
                f"cp p4rt_manager.py {MANAGER} 2>/dev/null || "
                f"cp /p4rt_manager.py {MANAGER} || true",
                f"chmod +x {MANAGER} || true",
            ],
            "fabric_mgr.startup",
        )

        for host_meta in host_metas:
            self.lab.create_file_from_list(
                host_meta.cmd_list, f"{host_meta.machine.name}.startup"
            )

        self.load_machines()

    def deploy(self):
        super().deploy()
        from nika.net_env.p4_dc_fabric.fabric_manager import reconcile_fabric

        reconcile_fabric(self._build_runtime(), self.model)

    def _frontend(self):
        return next(w for w in self.model.web_endpoints() if w.leaf_id == QUERY_LEAF)

    def _shards(self):
        return [w for w in self.model.web_endpoints() if w.leaf_id != QUERY_LEAF][
            :QUERY_SHARDS
        ]

    def business_workload(self) -> BusinessWorkload:
        clients = [e.name for e in self.model.client_endpoints()]
        webs = [e.ip for e in self.model.web_endpoints()]
        frontend = self._frontend()
        return BusinessWorkload(
            processes=(
                *client_processes(
                    clients, webs, page_pause="5-20", large_pause="180-420"
                ),
                *fanout_processes(
                    frontend.name,
                    {w.name: w.ip for w in self._shards()},
                    count=SHARD_ANSWER_DATAGRAMS,
                    size=SHARD_DATAGRAM_BYTES,
                    period="0.5-1.5",
                ),
            ),
            exercises=(
                WorkloadExercise(FAN_IN_BURSTS, (frontend.name,)),
                *bulk_downloads(clients),
            ),
        )

    def target_roles(self) -> dict[str, list[str]]:
        roles = super().target_roles()
        clients = [h for h in roles["hosts"] if "client" in h] or roles["hosts"]
        return {
            **roles,
            "hosts": clients,
            "host1_pool": clients,
            "attacker_pool": clients,
            "web": roles["web"] or clients,
            "l2_endpoints": sorted(clients, key=lambda h: h != "client_1_1"),
        }

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        # Unknown sizes fall back to the small fabric's path.
        model = build_clos_fabric_model(
            topo_size if topo_size in {"s", "m", "l"} else "s"
        )
        clients = model.client_endpoints()
        src = clients[0]
        dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
        # A peer on a third leaf: its path to dst crosses the spines and dst's
        # leaf but not src's leaf, so it controls for faults on src's leaf.
        peer = next(
            (c for c in clients if c.leaf_id not in {src.leaf_id, dst.leaf_id}),
            next((c for c in clients if c.name != src.name), None),
        )
        return ProbePath(
            src_host=src.name,
            dst_ip=dst.ip,
            http_url=f"http://{dst.ip}/",
            control_plane_host="leaf_1",
            peer_host=peer.name if peer else None,
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.p4_dc_fabric.verify import verify_p4_dc_fabric_lab_startup

        return verify_p4_dc_fabric_lab_startup(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            model=self.model,
        )

    def verify_lab(self) -> dict:
        from nika.net_env.p4_dc_fabric.verify import verify_p4_dc_fabric_lab

        return verify_p4_dc_fabric_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            model=self.model,
        )
