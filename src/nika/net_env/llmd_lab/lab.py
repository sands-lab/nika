"""LLM disaggregated inference lab (llmd-lab).

A star topology with Kubernetes (k3s) deploying llm-d with disaggregated Prefill/Decode.
All nodes connect to a single bridged switch; the host stages workload images.
"""

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import yaml

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import REPO_ROOT, RUNTIME_DIR
from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.utils.k8s_workload_cache import (
    K3S_IMAGE,
    K3S_SYSTEM_IMAGES,
    mount_workload_cache,
)
from nika.runtime.spec import NodeRole
from nika.utils.net import pick_free_port
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

cur_path = os.path.dirname(os.path.abspath(__file__))

_K3S_IMAGE = K3S_IMAGE
_BASE_IMAGE = nika_image("base")

_KUBECONFIG_REMOTE_PATH = "/etc/rancher/k3s/k3s.yaml"

_K3S_ULIMITS = ["nproc=65535", "nofile=65535"]
_HELM_VERSION = "v3.21.3"
_HELM_ARCHIVE_SHA256 = {
    "amd64": "15e041a93a590dce8100f39385cd98c84a765c9e36aeeb9e2dc6ff9e4769e2e0",
    "arm64": "67f58155079ff9ffab98ba5c88daff0ed9b542f3a4732f5dd426dde7dd0f5244",
}
machine = platform.machine().lower()
if machine in ("x86_64", "amd64"):
    _HELM_ARCHITECTURE = "amd64"
elif machine in ("aarch64", "arm64"):
    _HELM_ARCHITECTURE = "arm64"
else:
    raise RuntimeError(f"Unsupported Helm host architecture: {machine}")
_HELM_ARCHIVE_URL = (
    f"https://get.helm.sh/helm-{_HELM_VERSION}-linux-{_HELM_ARCHITECTURE}.tar.gz"
)
_AGENTGATEWAY_VERSION = "v1.1.0"
_HELM_CHART_SHA256 = {
    "agentgateway-crds": "abc114babffc70061248d1526c0508357c39cfaada00640d1b60f4bc6cad3a1e",
    "agentgateway": "4c04f0ae3fc01869fd49d41cbbb246377e8a7c3d3b6ac7c665f2bbc68cf6c2b6",
}
_HELM_CHART_SPECS = (
    ("agentgateway-crds", "oci://cr.agentgateway.dev/charts/agentgateway-crds"),
    ("agentgateway", "oci://cr.agentgateway.dev/charts/agentgateway"),
)


def _ensure_helm_binary() -> Path:
    """Download Helm on the host (k3s busybox wget has no HTTPS) and stage it for Kathara."""
    cache_dir = REPO_ROOT / ".nika_cache" / "helm" / _HELM_VERSION
    helm_bin = cache_dir / f"helm-{_HELM_ARCHITECTURE}"
    if helm_bin.is_file() and os.access(helm_bin, os.X_OK):
        return helm_bin

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "helm.tgz"
        with (
            urllib.request.urlopen(_HELM_ARCHIVE_URL, timeout=60) as response,
            archive.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        with archive.open("rb") as handle:
            if (
                hashlib.file_digest(handle, "sha256").hexdigest()
                != _HELM_ARCHIVE_SHA256[_HELM_ARCHITECTURE]
            ):
                raise RuntimeError(
                    "Helm archive checksum does not match the pinned release"
                )
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(f"linux-{_HELM_ARCHITECTURE}/helm")
            tar.extract(member, path=tmp, filter="data")
        extracted = Path(tmp) / f"linux-{_HELM_ARCHITECTURE}" / "helm"
        shutil.copy2(extracted, helm_bin)
        helm_bin.chmod(0o755)
    return helm_bin


def ensure_helm_charts() -> list[Path]:
    """Download AgentGateway Helm charts on the host and return cached tgz paths."""
    import subprocess

    cache_dir = REPO_ROOT / ".nika_cache" / "helm" / "charts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    helm_bin = _ensure_helm_binary()
    chart_paths: list[Path] = []
    for chart_name, oci_url in _HELM_CHART_SPECS:
        chart_path = cache_dir / f"{chart_name}-{_AGENTGATEWAY_VERSION}.tgz"
        if not chart_path.is_file():
            with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
                subprocess.run(
                    [
                        str(helm_bin),
                        "pull",
                        oci_url,
                        "--version",
                        _AGENTGATEWAY_VERSION,
                        "-d",
                        tmp,
                    ],
                    check=True,
                    timeout=120,
                )
                staged = Path(tmp) / chart_path.name
                with staged.open("rb") as handle:
                    if (
                        hashlib.file_digest(handle, "sha256").hexdigest()
                        != _HELM_CHART_SHA256[chart_name]
                    ):
                        raise RuntimeError(
                            f"Helm chart checksum mismatch: {chart_name}"
                        )
                staged.replace(chart_path)
        with chart_path.open("rb") as handle:
            if (
                hashlib.file_digest(handle, "sha256").hexdigest()
                != _HELM_CHART_SHA256[chart_name]
            ):
                raise RuntimeError(f"Helm chart cache checksum mismatch: {chart_path}")
        chart_paths.append(chart_path)
    return chart_paths


class LLMDInferenceCluster(NetworkEnvBase):
    LAB_NAME = "llmd_lab"
    K8S_HOST_IMAGES = (_K3S_IMAGE, _BASE_IMAGE)
    K8S_WORKLOAD_IMAGES = (
        *K3S_SYSTEM_IMAGES,
        "quay.io/metallb/controller:v0.16.1@sha256:f51ab515de9ccd20dc3dccb093e48df8adddac019326c456f449e55ba91b6420",
        "quay.io/metallb/speaker:v0.16.1@sha256:16561e96531e1852d5c229ad7fae6e994dcfa983ff7f4de6b6208b34a4e2ddbc",
        "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0@sha256:873179822ab0895a37ea09f2112ca39a6ae50a26612561c8bfad7f9a8c5af6f5",
        "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0@sha256:4cc3f15f254c26df7611e3b92ff7c82f83ad4ecb325de639c9dfb32873c6ce90",
        "ghcr.io/llm-d/llm-d-inference-sim:latest@sha256:32144df791330a0006b747edfdf2b114a0fe728e023a9d1b3463eeb48d32abb9",
        # agentgateway Helm chart v1.1.0 (_AGENTGATEWAY_VERSION):
        # the controller defaults to the chart appVersion and deploys proxies
        # with the same release tag.
        "cr.agentgateway.dev/controller:v1.1.0@sha256:0c4179780a3353a20f403ed51c764127f2806b6aa1a5ecb6da7655b5b27d7926",
        "cr.agentgateway.dev/agentgateway:v1.1.0@sha256:b5fd647604aa37eb2da372206a6681dfd613461e2445d89ba0b80e8439a4ff29",
    )
    VERIFY_MAX_WAIT_SEC = 1800
    VERIFY_RETRY_DELAY_SEC = 2
    TOPO_LEVEL = "hard"

    @classmethod
    def prepare_k8s_image_cache(cls) -> None:
        ensure_helm_charts()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A star-topology Kubernetes (k3s) cluster running llm-d with disaggregated "
            "Prefill/Decode inference. All workload images are prepared on the host; "
            "uses Gateway API and inference extensions."
        )
        self.kubernetes_nodes = []

        # K3s machines: name -> (links, is_controller)
        _k3s_machines = {
            "controller": (["A"], True),
            "worker1": (["A"], False),
            "worker2": (["A"], False),
            "worker3": (["A"], False),
            "worker4": (["A"], False),
            "worker5": (["A"], False),
        }

        all_machines = {}

        # Keepalive until device startup signals networking is ready, then exec
        # k3s as PID1 (avoids bridge/default-route race and cgroupv2 issues; #38).
        _k3s_wait = "while [ ! -f /var/run/nika-net-ready ]; do sleep 1; done; "
        _k3s_server = (
            "server --disable servicelb --disable traefik --write-kubeconfig-mode 644 "
            "--disable-default-registry-endpoint"
        )
        for name, (links, is_controller) in _k3s_machines.items():
            m = self.lab.new_machine(name, **{"image": _K3S_IMAGE})
            mount_workload_cache(m, self.LAB_NAME)
            self.declare_machine(
                name,
                role=(
                    NodeRole.CONTROLLER if is_controller else NodeRole.INFRASTRUCTURE
                ),
                capabilities=("linux", "k3s"),
            )
            m.add_meta("privileged", True)
            m.add_meta("bridged", True)
            for ulimit in _K3S_ULIMITS:
                m.add_meta("ulimit", ulimit)
            m.add_meta("shell", "/bin/sh")
            m.add_meta("entrypoint", "/bin/sh")
            if is_controller:
                m.add_meta(
                    "args",
                    f'-c "{_k3s_wait}exec /bin/k3s {_k3s_server}"',
                )
                m.add_meta("env", "K3S_TOKEN=secret")
                m.add_meta("env", "VERIFY_CHECKSUM=false")
                m.add_meta("env", "KUBECONFIG=/etc/rancher/k3s/k3s.yaml")
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
                m.add_meta("env", "K3S_URL=https://controller:6443")
                m.add_meta("env", "K3S_TOKEN=secret")
            for link in links:
                self.lab.connect_machine_to_link(name, link)
            all_machines[name] = m

        # Client machine for testing service reachability
        client = self.lab.new_machine("client", **{"image": _BASE_IMAGE})
        self.declare_machine(
            client.name,
            role=NodeRole.HOST,
            capabilities=("linux",),
            reachability_target=True,
        )
        self.lab.connect_machine_to_link("client", "A")
        all_machines["client"] = client

        # Dedicated HTTP endpoint for application-layer faults (separate from
        # the probe client so CPU-quota injects do not starve curl itself).
        # nika/base includes stress-ng; pin NanoCpus at create time so recover
        # can restore a non-zero quota (Docker ignores NanoCpus:0 clears).
        web = self.lab.new_machine(
            "web", **{"image": nika_image("base"), "cpus": 1.0, "mem": "512m"}
        )
        self.declare_machine(
            web.name,
            role=NodeRole.SERVICE,
            capabilities=("linux",),
            service_type="web",
        )
        self.lab.connect_machine_to_link("web", "A")
        all_machines["web"] = web

        # Inject Helm into controller FS from host cache (busybox wget cannot fetch HTTPS).
        helm_bin = _ensure_helm_binary()
        all_machines["controller"].create_file_from_path(
            str(helm_bin), "/usr/local/bin/helm"
        )
        # Stage charts now; the preload-time cache_scenario() runs after
        # machine files are staged, and BusyBox wget cannot fetch HTTPS.
        chart_paths = ensure_helm_charts()
        gateway_images = {
            image.split("@")[0].split("/")[-1].split(":")[0]: image.split("/")[
                -1
            ].split(":", 1)[1]
            for image in self.K8S_WORKLOAD_IMAGES
            if image.startswith("cr.agentgateway.dev/")
        }
        all_machines["controller"].create_file_from_string(
            yaml.safe_dump(
                {
                    "controller": {"image": {"tag": gateway_images["controller"]}},
                    "proxy": {"image": {"tag": gateway_images["agentgateway"]}},
                    "inferenceExtension": {"enabled": True},
                    "image": {"pullPolicy": "Never"},
                }
            ),
            "/helm-charts/values.yaml",
        )
        for chart_path in chart_paths:
            all_machines["controller"].create_file_from_path(
                str(chart_path), f"/helm-charts/{chart_path.name}"
            )

        all_machines["controller"].create_file_from_path(
            str(Path(__file__).resolve().parent.parent / "utils" / "k8s_bootstrap.sh"),
            "/nika-bootstrap.sh",
        )

        # Load per-machine configuration directories and startup scripts
        for name, m in all_machines.items():
            machine_dir = os.path.join(cur_path, name)
            if os.path.isdir(machine_dir):
                m.copy_directory_from_path(machine_dir, "/")
            startup_file = os.path.join(cur_path, f"{name}.startup")
            if os.path.isfile(startup_file):
                self.lab.create_file_from_path(startup_file, f"{name}.startup")

        self.load_machines()

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
            "routers": roles["k8s_controllers"] or clients,
            "web": roles["web"] or clients,
            "controllers": roles["k8s_controllers"],
            "l2_endpoints": sorted(clients, key=lambda h: h != "client"),
        }

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="client",
            dst_ip="200.0.0.8",
            http_url="http://200.0.0.8/",
            control_plane_host="controller",
            peer_host="web",
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.llmd_lab.verify import verify_llmd_lab_startup

        return verify_llmd_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )

    def verify_lab(self) -> dict:
        from nika.net_env.llmd_lab.verify import verify_llmd_lab

        return verify_llmd_lab(self._build_runtime(), scenario_name=self.LAB_NAME)

    def sync_client_hosts(self) -> None:
        from nika.net_env.utils.k8s_client_hosts import sync_llmd_client_hosts

        sync_llmd_client_hosts(self._build_runtime())

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
