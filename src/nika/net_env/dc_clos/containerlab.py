"""Materialize the DC Clos design on Containerlab vendor routers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import subprocess
import time
import tempfile
from types import SimpleNamespace

import yaml
from docker.utils import parse_bytes

from nika.config import clab_workdir
from nika.net_env.dc_clos.lab import DCClos
from nika.net_env.isp.containerlab.lab import SRL_IMAGE, SRL_TYPE, SRL_PASSWORD
from nika.net_env.isp.traffic.stubs import PlannedHost
from nika.net_env.synthesis.netlab import run_netlab
from nika.net_env.synthesis.netlab_kathara import FRR_MULTIPATH, kathara_topology
from nika.net_env.synthesis.rendered import stored_configs
from nika.net_env.synthesis.srl_updates import srl_config_document
from nika.net_env.utils.containerlab.mgmt_subnet import (
    mgmt_ipv4_addresses,
    mgmt_ipv4_network,
)
from nika.net_env.utils.sonic.common import (
    KIND as SONIC_KIND,
    PORT_MTU,
    activate_sonic_routers,
    ensure_sonic_image,
    sonic_port,
)
from nika.net_env.utils.sonic.render import (
    clab_kind,
    translate_frr_design,
    write_node_files,
)
from nika.net_env.verify import FRR_BGP_PEERS, BgpPeerQuery, srl_bgp_established_peers
from nika.runtime.containerlab.parse import parse_clab_topology
from nika.runtime.spec import MachineInventory, NodeIdentity, NodeRole
from nika.utils.dependencies import require_gnmic
from nika.utils.parallel import bounded_parallel_map


class ContainerlabDCClos(DCClos):
    """Use DCClos's in-memory design and endpoint files on a vendor backend.

    The parent authors the topology, routing design and services once. Only
    router configuration and deployment change; no Kathara lab is deployed.
    """

    ROUTERS_CHECK_DST_MAC = True
    NATIVE_PROFILE_ROLES = {}
    NATIVE_PROFILE_SIZES = {}
    VERIFY_MAX_WAIT_SEC = 600

    def __init__(
        self,
        topo_size="s",
        device_profile=None,
        rendered=True,
        routing_parameters=None,
        **kwargs,
    ):
        profile = device_profile or "nokia_srlinux"
        if profile not in {"nokia_srlinux", "sonic"}:
            raise ValueError(f"Unsupported Containerlab device profile: {profile}")
        kwargs.pop("backend", None)
        # Reuse the authored routing design and endpoint filesystem. This
        # builds an in-memory lab, with no runtime or deployment side effects.
        super().__init__(topo_size, backend="kathara", rendered=False, **kwargs)
        self._routing_topology, self._expected_peers = kathara_topology(
            self, routing_parameters
        )
        self._fabric_edges = DCClos.get_topology(self)
        self._design_lab = self.lab
        self.backend = "containerlab"
        self.device_profile = profile
        self.sonic = profile == "sonic"
        self.ROUTING_MODELS = frozenset({"sonic"}) if self.sonic else frozenset()
        for name in self.routers:
            self.machine_identities[name] = NodeIdentity(
                role=NodeRole.ROUTER,
                capabilities=("linux", profile, "bgp")
                + (("frr",) if self.sonic else ("srl",)),
            )
        self.load_machines()
        self.metadata["machine_identities"] = MachineInventory(
            self.machine_identities
        ).to_dict()
        self.lab = None
        if routing_parameters is not None:
            with tempfile.TemporaryDirectory() as work:
                from pathlib import Path

                self._configs = self.render_netlab_configs(Path(work))
        else:
            self._configs = dict(stored_configs(self)) if rendered else {}
        self.traffic = SimpleNamespace(
            hosts=tuple(
                PlannedHost(
                    e.name,
                    f"leaf_{e.leaf_id}",
                    str(e.leaf_id),
                    f"rack_{e.leaf_id}",
                    e.ip,
                    f"10.0.{e.leaf_id}.1",
                    f"10.0.{e.leaf_id}.0/24",
                    24,
                    (),
                )
                for e in self.model.endpoints
            )
        )

    def load_machines(self):
        if self.lab is not None:
            super().load_machines()

    def _interface(self, name):
        if name == "br0":
            return "eth0" if self.sonic else "irb0"
        index = int(name.removeprefix("eth")) + 1
        return f"eth{index}" if self.sonic else f"ethernet-1/{index}"

    def netlab_render_input(self):
        topology = deepcopy(self._routing_topology)
        topology["defaults.device"] = "frr" if self.sonic else "srlinux"
        for node in topology["nodes"].values():
            if not self.sonic:
                if not node.get("routing"):
                    node["module"].remove("routing")
                node.update(
                    {
                        "box": SRL_IMAGE,
                        "clab.type": SRL_TYPE,
                        "loopback": {"ipv4": f"{node['bgp.router_id']}/32"},
                    }
                )
        for link in topology["links"]:
            for endpoint in link["interfaces"]:
                endpoint["ifname"] = self._interface(endpoint["ifname"])
        return topology

    def render_netlab_configs(self, directory):
        configs, model = run_netlab(self.netlab_render_input(), directory)
        peers = {
            (name, str(peer["ipv4"]), int(peer["as"]))
            for name, node in model["nodes"].items()
            for peer in node.get("bgp", {}).get("neighbors", [])
        }
        if peers != self._expected_peers:
            raise ValueError("netlab changed the DC Clos BGP sessions")
        origins = {
            (name, p["ipv4"])
            for name, node in model["nodes"].items()
            for p in node.get("bgp", {}).get("advertise", [])
        }
        if origins != {
            (node, prefix) for prefix, node in self.bgp_origin_prefixes().items()
        }:
            raise ValueError("netlab changed the DC Clos prefix origins")
        expected = {
            (e["node"], e["ifname"], e["ipv4"])
            for link in self.netlab_render_input()["links"]
            for e in link["interfaces"]
        }
        actual = {
            (name, i["ifname"], i["ipv4"])
            for name, node in model["nodes"].items()
            for i in node.get("interfaces", [])
        }
        if actual != expected:
            raise ValueError("netlab changed the DC Clos interfaces or addresses")
        if self.sonic:
            for name in configs:
                router = self.routing_design.routers[name]
                configs[name] += FRR_MULTIPATH.format(
                    asn=router.asn, paths=router.bgp_multipath
                )
        else:
            configs = {
                name: yaml.safe_dump(doc, sort_keys=False)
                for name, doc in self._srl_documents(configs).items()
            }
        return configs

    @property
    def routing_documents(self):
        if self.sonic:
            return None
        return getattr(self, "_completed_routing_documents", None) or {
            n: srl_config_document(c) for n, c in self._configs.items()
        }

    def _srl_documents(self, configs):
        if self.sonic:
            return None
        documents = {n: srl_config_document(c) for n, c in configs.items()}
        for name, document in documents.items():
            # netlab 26.10's SRL BGP macro requires loopback data even for
            # direct eBGP. The design has no loopbacks; remove that template input.
            document["srl_nokia-interfaces:interface"] = [
                i
                for i in document["srl_nokia-interfaces:interface"]
                if i["name"] != "system0"
            ]
            for interface in document["srl_nokia-interfaces:interface"]:
                interface["admin-state"] = "enable"
            instance = document["srl_nokia-network-instance:network-instance"][0]
            instance["admin-state"] = "enable"
            instance["interface"] = [
                i for i in instance["interface"] if i["name"] != "system0.0"
            ]
            bgp = instance["protocols"]["srl_nokia-bgp:bgp"]
            for family in bgp["afi-safi"]:
                family["multipath"] = {
                    "allow-multiple-as": True,
                    "maximum-paths": self.routing_design.routers[name].bgp_multipath,
                }
        for leaf in self.model.leaves:
            document = documents[leaf]
            ports = [p for p in self.model.ports[leaf] if p.role == "host"]
            document["srl_nokia-interfaces:interface"].extend(
                {
                    "name": self._interface(p.name),
                    "admin-state": "enable",
                    "subinterface": [
                        {"index": 0, "type": "bridged", "admin-state": "enable"}
                    ],
                }
                for p in ports
            )
            document["srl_nokia-network-instance:network-instance"].append(
                {
                    "name": "rack",
                    "type": "mac-vrf",
                    "admin-state": "enable",
                    "interface": [
                        {"name": f"{self._interface(p.name)}.0"} for p in ports
                    ]
                    + [{"name": "irb0.0"}],
                }
            )
        return documents

    @property
    def sonic_routers(self):
        routers = {}
        for name, config in self._configs.items():
            router = translate_frr_design(
                name,
                config,
                lambda iface: "Vlan100" if iface == "eth0" else sonic_port(iface),
            )
            access = {
                sonic_port(self._interface(p.name)): PORT_MTU
                for p in self.model.ports[name]
                if p.role == "host"
            }
            routers[name] = replace(
                router,
                ports={**router.ports, **access},
                vlans={"Vlan100": tuple(access)} if access else {},
            )
        return routers

    def _document(self):
        addresses = mgmt_ipv4_addresses(self.name, len(self.routers))
        self._mgmt = dict(zip(self.routers, addresses, strict=True))
        nodes = {
            n: {"kind": SONIC_KIND}
            if self.sonic
            else {"kind": "nokia_srlinux", "type": SRL_TYPE}
            for n in self.routers
        }
        for n in self.routers:
            nodes[n]["mgmt-ipv4"] = self._mgmt[n]
        for e in self.model.endpoints:
            machine = self._design_lab.machines[e.name]
            nodes[e.name] = {
                "kind": "linux",
                "image": machine.get_image(),
                "cmd": "sleep infinity",
                "network-mode": "none",
                "cpu": machine.get_cpu(1_000_000_000) / 1_000_000_000,
                "memory": f"{parse_bytes(machine.get_mem())}b",
            }
        links = []
        for a, b in self._fabric_edges:
            ends = []
            for endpoint in (a, b):
                node, iface = endpoint.split(":")
                ends.append(
                    f"{node}:{self._interface(iface).replace('ethernet-1/', 'e1-') if node in self.routers else iface}"
                )
            links.append({"endpoints": ends})
        return {
            "name": self.name,
            "mgmt": {
                "network": f"br-{self.name}",
                "ipv4-subnet": str(mgmt_ipv4_network(self.name, len(self.routers))),
            },
            "topology": {
                "kinds": {SONIC_KIND: clab_kind()}
                if self.sonic
                else {"nokia_srlinux": {"image": SRL_IMAGE}},
                "nodes": nodes,
                "links": links,
            },
        }

    def get_lab_spec(self):
        return parse_clab_topology(self._document())

    def get_topology(self):
        return [link.endpoints for link in self.get_lab_spec().links]

    def _topology_segments(self):
        if self.lab is not None:
            return super()._topology_segments()
        return self.get_topology()

    def _prepare_runtime_files(self):
        self.runtime_workdir = clab_workdir(self.name)
        self.topology_file = self.runtime_workdir / "dc_clos.clab.yml"
        self.topology_file.write_text(yaml.safe_dump(self._document(), sort_keys=False))
        root = self.runtime_workdir / "configs"
        root.mkdir(exist_ok=True)
        if self.sonic:
            for router in self.sonic_routers.values():
                write_node_files(root, router)
        else:
            for node, document in self.routing_documents.items():
                (root / f"{node}.yaml").write_text(
                    yaml.safe_dump(document, sort_keys=False)
                )

    def deploy(self):
        if self.sonic:
            ensure_sonic_image()
        else:
            require_gnmic()
        self._ensure_docker_images()
        self._ensure_runtime_files()
        runtime = self._build_runtime()
        if runtime.exists():
            return
        super().deploy()
        if self.sonic:
            activate_sonic_routers(
                runtime, {n: r.daemons for n, r in self.sonic_routers.items()}
            )
        else:
            bounded_parallel_map(self._configure_srl, self.routers)
        bounded_parallel_map(
            self._configure_endpoint, [e.name for e in self.model.endpoints]
        )

    def _configure_srl(self, node):
        deadline = time.monotonic() + 600
        command = [
            "gnmic",
            "-a",
            f"{self._mgmt[node]}:57400",
            "--timeout",
            "30s",
            "-u",
            "admin",
            "-p",
            SRL_PASSWORD,
            "-e",
            "json_ietf",
            "--skip-verify",
            "set",
            "--update-path",
            "/",
            "--update-file",
            str(self.runtime_workdir / "configs" / f"{node}.yaml"),
        ]
        while True:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                self._build_runtime().exec(node, 'sr_cli "save startup"', timeout=30)
                return
            if (
                any(code in result.stderr for code in ("InvalidArgument", "NotFound"))
                or time.monotonic() >= deadline
            ):
                raise RuntimeError(
                    f"SR Linux configuration failed on {node}: {result.stderr}"
                )
            time.sleep(5)

    def _configure_endpoint(self, node):
        runtime = self._build_runtime()
        fs = self._design_lab.machines[node].fs
        for path in fs.walk.files():
            runtime.write_file(node, path, fs.readtext(path))
        script = self._design_lab.fs.readtext(f"{node}.startup")
        runtime.write_file(
            node, "/tmp/endpoint.sh", "set -e\nip link set eth0 mtu 1500\n" + script
        )
        output = runtime.exec(
            node,
            "bash /tmp/endpoint.sh; result=$?; rm /tmp/endpoint.sh; echo setup_rc=$result",
            timeout=60,
        )
        if "setup_rc=0" not in output:
            raise RuntimeError(f"Endpoint startup failed on {node}: {output}")

    def _bgp_peers(self):
        query = (
            FRR_BGP_PEERS
            if self.sonic
            else BgpPeerQuery(
                command='sr_cli "show network-instance default protocols bgp neighbor"',
                established_peers=srl_bgp_established_peers,
            )
        )
        return dict.fromkeys(self.routers, query)

    def seal_baseline(self):
        super().seal_baseline()
        if self.sonic:

            def remove_source(node):
                output = self._build_runtime().exec(
                    node,
                    "if mountpoint -q /etc/sonic/startup; then umount -l /etc/sonic/startup || exit 1; fi; "
                    "test ! -e /etc/sonic/startup/frr.conf && echo sources_removed",
                )
                if "sources_removed" not in output:
                    raise RuntimeError(
                        f"Cannot remove SONiC boot sources on {node}: {output}"
                    )

            bounded_parallel_map(remove_source, self.routers)

    def _collect_lab_images(self):
        return {
            self._design_lab.machines[e.name].get_image() for e in self.model.endpoints
        }

    def verify_lab(self):
        from nika.net_env.dc_clos.verify import verify_dc_clos_lab

        return verify_dc_clos_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            nodes=tuple(self.machine_identities),
            bgp_neighbors=self.bgp_neighbors,
            client_addresses=self.client_addresses,
            dns_servers=self.dns_servers,
            web_urls=tuple(f"http://{name}" for name in self.dns_records),
            bgp_peers=self._bgp_peers(),
        )

    def startup_verify_lab(self):
        return self.verify_lab()
