"""Kubernetes fat-tree BGP lab (k8s-lab).

A two-pod fat-tree topology with BGP routing and Kubernetes (k3s) services.
The lab includes FRR routers for BGP routing and k3s nodes for Kubernetes.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import RUNTIME_DIR
from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.k8s_lab.topology import K8sTopology
from nika.net_env.utils.k8s_traffic_pods import K8sTrafficPods
from nika.net_env.utils.k8s_workload_cache import (
    K3S_NODE_IMAGE,
    K3S_SYSTEM_IMAGES,
    mount_workload_cache,
)
from nika.runtime.spec import NodeRole
from nika.traffic.workload import (
    BusinessWorkload,
    WorkloadProcess,
    routine_change_processes,
)
from nika.utils.net import pick_free_port
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

cur_path = os.path.dirname(os.path.abspath(__file__))

_FRR_IMAGE = nika_image("frr")
_K3S_IMAGE = K3S_NODE_IMAGE
_BASE_IMAGE = nika_image("base")

# Per-node monitoring agent (``shared/uptime-agent.yaml``).
_AGENT_IMAGE = "busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092"

_WORD_IMAGE = "ik2227/word:latest@sha256:a32c1a461340d0880693ae1be5580108a24b8fb5b071902a8cb865b02c31a50d"

_KUBECONFIG_REMOTE_PATH = "/etc/rancher/k3s/k3s.yaml"

_K3S_ULIMITS = ["nproc=65535", "nofile=65535"]


class K8sFatTreeBGP(K8sTrafficPods, NetworkEnvBase):
    LAB_NAME = "k8s_lab"
    K8S_HOST_IMAGES = (_FRR_IMAGE, _K3S_IMAGE, _BASE_IMAGE)
    K8S_WORKLOAD_IMAGES = (
        *K3S_SYSTEM_IMAGES,
        "quay.io/metallb/controller:v0.14.9@sha256:86261567e5ff03978893bf03ea865275283ad1e3f0f20dd342ed501b651fdf78",
        "quay.io/metallb/speaker:v0.14.9@sha256:b09a1dfcf330938950b65115cd58f6989108c0c21d3c096040e7fe9a25a92993",
        "quay.io/frrouting/frr:9.1.0@sha256:f310c2ebb3827fa03b9674ee05e70a7d5eef2123bcc3b475eb2ef14dafcb52b4",
        "registry.k8s.io/ingress-nginx/controller:v1.12.0@sha256:e6b8de175acda6ca913891f0f727bca4527e797d52688cbe9fec9040d6f6b6fa",
        "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.5.0@sha256:aaafd456bda110628b2d4ca6296f38731a3aaf0bf7581efae824a41c770a8fc4",
        "postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54",
        _WORD_IMAGE,
        "ik2227/weather:latest@sha256:b69236a70d439acad840ce5cf71e01bff3be6334095e3887765bba68ec81b35e",
        _AGENT_IMAGE,
    )
    # The word app image (Python 3) runs traffic programs on the k3s nodes.
    TRAFFIC_POD_IMAGE = _WORD_IMAGE
    VERIFY_MAX_WAIT_SEC = 1800
    VERIFY_RETRY_DELAY_SEC = 15
    TOPO_LEVEL = "hard"

    def __init__(self, topo_size: str = "m", **kwargs):
        super().__init__(**kwargs)
        self.topo_size = topo_size
        self.topology = K8sTopology(topo_size)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A two-pod fat-tree topology using EBGP routing (FRR), with Kubernetes (k3s) "
            "services deployed across the cluster. The network uses leaf-spine-core architecture "
            "with BGP for routing and k3s for container orchestration. "
            "dc_exit is a plain Linux router with static routes (no routing daemon) "
            "between the upstream AS routers and the data center. MetalLB "
            "LoadBalancer addresses (101.0.0.0/8) are advertised by the k3s workers "
            "over BGP and are served on their service ports only: each worker keeps "
            "a static discard route for the pool (101.0.0.0/8) so pool addresses "
            "answer only on their service ports, and only the node serving the "
            "ingress pod announces the VIP. The client resolves "
            "datacenter.com through a static /etc/hosts entry; "
            "the lab runs no DNS server for lab names (cluster DNS serves pods only). "
            "The external client uses the word and weather web apps through the "
            "ingress at datacenter.com at all times, and a monitoring agent on "
            "every k3s node calls the apps through their Service names. "
            "The pod-2 leaves (leaf_2_*) host no Kubernetes nodes; they discard-route "
            "and announce the unused 101.0.0.0/8 and 201.0.0.0/8 aggregates. "
            # kubectl is only usable on the k3s server. Resource ids use device names or Kubernetes objects.
            "`kubectl` works only on controller (the k3s server); the worker "
            "devices ship the binary but have no kubeconfig. Resources are lab "
            "devices (identified by device name) or Kubernetes objects "
            "(identified by kind, namespace and name, as listed in the "
            "resource inventory)."
        )
        self.kubernetes_nodes = []
        # Devices whose local PersistentVolumes back the business databases.
        self.kubernetes_storage_nodes = list(self.topology.storage_nodes)

        # --- FRR router machines: name -> (link order, extra_metas) ---
        # Link order determines eth index: first link = eth0, second = eth1, etc.
        _frr_machines = {
            "leaf_1_1": ["A", "B"],
            "leaf_1_2": ["D", "E"],
            "spine_1_1": ["A", "D", "G", "H"],
            "spine_1_2": ["B", "E", "I", "J"],
            "spine_2_1": ["K", "N", "Q", "R"],
            "spine_2_2": ["L", "O", "S", "T"],
            "leaf_2_1": ["K", "L", "F"],
            "leaf_2_2": ["N", "O", "P"],
            "core_1_1": ["G", "I", "Q", "S"],
            "core_1_2": ["H", "J", "R", "T"],
            "dc_exit": ["AC", "F", "P"],
            "as1r1": ["AC", "M"],
            "as2r1": ["W", "M"],
        }

        # K3s node machines: name -> link order
        access_links = (
            dict(
                zip(
                    ("controller", *self.topology.workers),
                    ("C", "U", "AA", "V", "Z", "AB"),
                    strict=True,
                )
            )
            if self.topo_size == "m"
            else {}
        )
        _k3s_machines = {}
        for leaf, nodes in self.topology.leaf_nodes.items():
            for node in nodes:
                link = access_links.get(node, f"access_{node}")
                _frr_machines[leaf].append(link)
                _k3s_machines[node] = [link]

        # Only the controller is bridged so
        # Docker can publish the API port to the host (host-side kubectl/MCP).
        _bridged = {"controller"}

        # Multipath sysctl for core and spine switches
        _sysctl_multipath = "net.ipv4.fib_multipath_hash_policy=1"
        _sysctl_machines = {
            "core_1_1",
            "core_1_2",
            "spine_1_1",
            "spine_1_2",
            "spine_2_1",
            "spine_2_2",
            "leaf_1_1",
            "leaf_1_2",
            "leaf_2_1",
            "leaf_2_2",
            "dc_exit",
        }

        # IPv6 required for FRR unnumbered interfaces
        _ipv6_machines = {
            "core_1_1",
            "core_1_2",
            "spine_1_1",
            "spine_1_2",
            "spine_2_1",
            "spine_2_2",
            "leaf_1_1",
            "leaf_1_2",
            "leaf_2_1",
            "leaf_2_2",
        }

        # Routers that run the FRR image but only carry kernel static routes.
        _static_routers = {"dc_exit"}

        all_machines = {}

        # Create FRR router machines
        for name, links in _frr_machines.items():
            m = self.lab.new_machine(name, **{"image": _FRR_IMAGE})
            self.declare_machine(
                name,
                role=NodeRole.ROUTER,
                capabilities=(
                    ("linux", "static_routing")
                    if name in _static_routers
                    else ("linux", "frr", "bgp")
                ),
            )
            if name in _bridged:
                m.add_meta("bridged", True)
            if name in _sysctl_machines:
                m.add_meta("sysctl", _sysctl_multipath)
            if name in _ipv6_machines:
                m.add_meta("ipv6", True)
            for link in links:
                self.lab.connect_machine_to_link(name, link)
            all_machines[name] = m

        # Create k3s node machines.
        # Keepalive until device startup signals networking is ready, then exec
        # k3s as PID1 (avoids bridge/default-route race and cgroupv2 issues; #38).
        _k3s_wait = "while [ ! -f /var/run/net-ready ]; do sleep 1; done; "
        controller_ip = self.topology.addresses["controller"]
        _k3s_server = (
            "server --disable servicelb --disable traefik --write-kubeconfig-mode 644 "
            f"--disable-default-registry-endpoint --node-ip {controller_ip} "
            f"--advertise-address {controller_ip} --tls-san {controller_ip}"
        )
        for name, links in _k3s_machines.items():
            m = self.lab.new_machine(name, **{"image": _K3S_IMAGE})
            mount_workload_cache(m, self.LAB_NAME)
            self.declare_machine(
                name,
                role=(
                    NodeRole.CONTROLLER
                    if name == "controller"
                    else NodeRole.INFRASTRUCTURE
                ),
                capabilities=("linux", "k3s"),
                reachability_target=True,
            )
            m.add_meta("privileged", True)
            for ulimit in _K3S_ULIMITS:
                m.add_meta("ulimit", ulimit)
            m.add_meta("shell", "/bin/sh")
            m.add_meta("entrypoint", "/bin/sh")
            if name == "controller":
                m.add_meta(
                    "args",
                    f'-c "{_k3s_wait}exec /bin/k3s {_k3s_server}"',
                )
                m.add_meta("env", "K3S_TOKEN=secret")
                # Expose kubectl (6443) on a host port unique to this lab instance,
                # so concurrent sessions don't collide on a fixed port mapping.
                controller_kubectl_port = pick_free_port()
                m.add_meta("port", f"{controller_kubectl_port}:6443/tcp")
                self.metadata["k8s_controller_port"] = controller_kubectl_port
            else:
                m.add_meta(
                    "args",
                    f'-c "{_k3s_wait}exec /bin/k3s agent --disable-default-registry-endpoint"',
                )
                # Match the server's advertised endpoint so agent load-balancer
                # discovery does not close in-flight bootstrap connections.
                m.add_meta("env", f"K3S_URL=https://{controller_ip}:6443")
                m.add_meta("env", "K3S_TOKEN=secret")
            if name in _bridged:
                m.add_meta("bridged", True)
            for link in links:
                self.lab.connect_machine_to_link(name, link)
            all_machines[name] = m

        # Create client machine
        client = self.lab.new_machine("client", **{"image": _BASE_IMAGE})
        self.declare_machine(
            client.name,
            role=NodeRole.HOST,
            capabilities=("linux",),
            reachability_target=True,
        )
        self.lab.connect_machine_to_link("client", "W")
        all_machines["client"] = client

        all_machines["controller"].create_file_from_path(
            str(Path(__file__).resolve().parent.parent / "utils" / "k8s_bootstrap.sh"),
            "/lab-bootstrap.sh",
        )

        # Load per-machine configuration directories and startup scripts
        for name, m in all_machines.items():
            machine_dir = os.path.join(cur_path, name)
            if os.path.isdir(machine_dir):
                m.copy_directory_from_path(machine_dir, "/")
            startup_file = os.path.join(cur_path, f"{name}.startup")
            if os.path.isfile(startup_file):
                self.lab.create_file_from_path(startup_file, f"{name}.startup")

        # Shared k8s manifests (services, deployments) are accessible via /shared on the controller
        shared_dir = os.path.join(cur_path, "shared")
        if os.path.isdir(shared_dir):
            all_machines["controller"].copy_directory_from_path(shared_dir, "/shared")

        self._render_topology_files(all_machines)
        self.load_machines()

    def _render_topology_files(self, machines) -> None:
        if self.topo_size == "m":
            return
        root = Path(cur_path)
        addresses = self.topology.addresses
        hosts = "127.0.0.1 localhost\n::1 localhost\n" + "".join(
            f"{address} {name}\n" for name, address in addresses.items()
        )
        for name, machine in machines.items():
            if name in addresses:
                machine.create_file_from_string(hosts, "/etc/hosts")
                machine.create_file_from_path(
                    str(root / "controller/etc/resolv.conf"), "/etc/resolv.conf"
                )
            if name.startswith("worker"):
                address = addresses[name]
                gateway = address.rsplit(".", 1)[0] + ".1"
                self.lab.create_file_from_string(
                    f"ip address add {address}/24 dev eth0\n"
                    f"ip route add default via {gateway} dev eth0\n"
                    "ip route add blackhole 101.0.0.0/8 proto static\n"
                    + (
                        "mkdir -p /mnt/data\n"
                        if name in self.topology.storage_nodes
                        else ""
                    )
                    + "mkdir -p /var/run\ntouch /var/run/net-ready\n",
                    f"{name}.startup",
                )
        for leaf_index, (leaf, nodes) in enumerate(self.topology.leaf_nodes.items(), 1):
            startup = (root / f"{leaf}.startup").read_text()
            startup = re.sub(r"ip address add 201\.[^\n]+\n", "", startup)
            access = "".join(
                f"ip address add 201.{leaf_index}.{i}.1/24 dev eth{i + 1}\n"
                for i in range(1, len(nodes) + 1)
            )
            self.lab.create_file_from_string(
                startup.replace("# Start FRR", access + "\n# Start FRR"),
                f"{leaf}.startup",
            )
            config = (root / leaf / "etc/frr/bgpd.conf").read_text()
            config = re.sub(r"  network 201\.[^\n]+\n", "", config)
            networks = "".join(
                f"  network 201.{leaf_index}.{i}.0/24\n"
                for i in range(1, len(nodes) + 1)
            )
            machines[leaf].create_file_from_string(
                config.replace("  maximum-paths", networks + "  maximum-paths"),
                "/etc/frr/bgpd.conf",
            )
        controller = machines["controller"]
        peers = list(
            yaml.safe_load_all((root / "controller/k8s/metallb-conf.yaml").read_text())
        )
        for peer, nodes in zip(
            peers[:2], self.topology.leaf_nodes.values(), strict=True
        ):
            peer["spec"]["nodeSelectors"][0]["matchExpressions"][0]["values"] = [
                n for n in nodes if n != "controller"
            ]
        controller.create_file_from_string(
            yaml.safe_dump_all(peers, sort_keys=False), "/k8s/metallb-conf.yaml"
        )
        volumes = list(
            yaml.safe_load_all((root / "controller/k8s/pv.yaml").read_text())
        )
        for volume, node in zip(volumes, self.topology.storage_nodes, strict=True):
            volume["metadata"]["name"] = f"k3s-pv-{node}"
            volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0][
                "matchExpressions"
            ][0]["values"] = [node]
        controller.create_file_from_string(
            yaml.safe_dump_all(volumes, sort_keys=False), "/k8s/pv.yaml"
        )
        ingress = list(
            yaml.safe_load_all((root / "controller/k8s/ingress-nginx.yaml").read_text())
        )
        for resource in ingress:
            if resource["kind"] == "Deployment":
                affinity = resource["spec"]["template"]["spec"]["affinity"][
                    "nodeAffinity"
                ]
                affinity["requiredDuringSchedulingIgnoredDuringExecution"][
                    "nodeSelectorTerms"
                ][0]["matchExpressions"][0]["values"] = list(self.topology.workers)
        controller.create_file_from_string(
            yaml.safe_dump_all(ingress, sort_keys=False), "/k8s/ingress-nginx.yaml"
        )
        startup = (
            (root / "controller.startup")
            .read_text()
            .replace("201.1.1.2", addresses["controller"])
            .replace("201.1.1.1", addresses["controller"].rsplit(".", 1)[0] + ".1")
            .replace("--replicas=2", f"--replicas={self.topology.app_replicas}")
        )
        self.lab.create_file_from_string(startup, "controller.startup")

    def _prepare_runtime_files(self) -> None:
        lab_name = self.name
        if not lab_name:
            raise ValueError("Lab name is required before deploy.")
        self.runtime_workdir = RUNTIME_DIR / "kathara" / lab_name
        self.runtime_workdir.mkdir(parents=True, exist_ok=True)

    def load_machines(self):
        super().load_machines()
        self.kubernetes_nodes = self.machine_inventory.names_for_capability("k3s")

    def target_roles(self) -> dict[str, list[str]]:
        roles = super().target_roles()
        clients = [h for h in roles["hosts"] if "client" in h] or roles["hosts"]
        return {
            **roles,
            "hosts": clients,
            "host1_pool": clients,
            "attacker_pool": clients,
            "routers": [r for r in roles["routers"] if "leaf" in r] or roles["routers"],
            "web": clients,
            "l2_endpoints": sorted(clients, key=lambda h: h != "client"),
        }

    def link_symptom_targets(self, node: str, intf: str) -> dict[str, str] | None:
        match = re.fullmatch(r"eth(\d+)", intf)
        machine = self.lab.machines.get(node)
        interface = machine.interfaces.get(int(match[1])) if machine and match else None
        if interface is None:
            return None
        for endpoint in sorted(interface.link.machines):
            if endpoint in self.topology.addresses:
                return {
                    "observer_device": "client",
                    "probe_dst_ip": self.topology.addresses[endpoint],
                }
        return None

    def restore_link_routes(self, runtime, host: str, interface: str) -> None:
        """Restore authored kernel routes flushed by an attachment outage."""
        startup = self.lab.fs.readtext(f"{host}.startup")
        routes = runtime.exec(host, "ip -4 route show").splitlines()
        for line in startup.splitlines():
            match = re.fullmatch(
                rf"ip route (?:add|append|replace) (.+ dev {re.escape(interface)})",
                line,
            )
            if match and match[1] not in routes:
                runtime.exec(host, f"ip route append {match[1]}")

    def http_backend_request(self, url: str) -> dict[str, str]:
        parsed = urlsplit(url)
        if parsed.path == "/weather" and not parsed.query:
            url += "?location=London"
        return {"url": url}

    def bgp_hijack_targets(self):
        from nika.net_env.isp.bgp.hijack import HijackTarget

        victim, observer_host, *_ = self.topology.leaf_nodes["leaf_1_2"]
        address = self.topology.addresses[victim]
        return [
            HijackTarget(
                hijacker="leaf_1_1",
                prefix=f"{address}/32",
                observer="leaf_1_2",
                observer_host=observer_host,
                probe_ip=address,
            )
        ]

    def business_workload(self) -> BusinessWorkload:
        # The external client browses the apps through the ingress VIP; the
        # in-cluster monitoring agents (``shared/uptime-agent.yaml``) call
        # them through cluster DNS.
        return BusinessWorkload(
            processes=(
                WorkloadProcess(
                    node="client",
                    app="api_client",
                    command="web-user",
                    args=(
                        "http://datacenter.com/word",
                        "http://datacenter.com/weather?location=London",
                        "--workers",
                        "2",
                        "--every",
                        "3-9",
                        "--seed",
                        "1",
                    ),  # fmt: skip
                ),
                *routine_change_processes(self.frr_routers()),
            ),
        )

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="client",
            dst_ip=K8sTopology(topo_size).addresses["controller"],
            http_url="http://datacenter.com/word",
            control_plane_host="controller",
            peer_host="as2r1",
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.k8s_lab.verify import verify_k8s_lab_startup

        return verify_k8s_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME, topo_size=self.topo_size
        )

    def verify_lab(self) -> dict:
        from nika.net_env.k8s_lab.verify import verify_k8s_lab

        return verify_k8s_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME, topo_size=self.topo_size
        )

    def sync_client_hosts(self) -> None:
        from nika.net_env.utils.k8s_client_hosts import sync_k8s_client_hosts

        sync_k8s_client_hosts(self._build_runtime())

    def post_deploy(self):
        self.sync_client_hosts()
        port = self.metadata.get("k8s_controller_port")
        if port is None:
            return
        from nika.net_env.utils.kathara.kubeconfig_export import (
            write_host_kubeconfig,
        )

        if self.runtime_workdir is None:
            self._prepare_runtime_files()
        write_host_kubeconfig(
            instance=self.instance,
            controller_machine=self.lab.machines["controller"],
            remote_kubeconfig_path=_KUBECONFIG_REMOTE_PATH,
            runtime_workdir=self.runtime_workdir,
            port=int(port),
            metadata=self.metadata,
        )
