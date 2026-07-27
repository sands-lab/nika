"""LLM disaggregated inference lab (llmd-lab).

A star topology with Kubernetes (k3s) deploying llm-d with disaggregated Prefill/Decode.
All nodes connect to a single bridged switch and use the internet for downloading models.
"""

import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import RUNTIME_DIR
from nika.net_env.base import NetworkEnvBase

cur_path = os.path.dirname(os.path.abspath(__file__))

_K3S_IMAGE = "rancher/k3s"
_BASE_IMAGE = "kathara/base"

_K3S_ULIMITS = ["nproc=65535", "nofile=65535"]
_HELM_VERSION = "v3.21.3"
machine = platform.machine().lower()
if machine in ("x86_64", "amd64"):
    _HELM_ARCHITECTURE = "amd64"
elif machine in ("aarch64", "arm64"):
    _HELM_ARCHITECTURE = "arm64"
else:
    _HELM_ARCHITECTURE = "amd64"
_HELM_ARCHIVE_URL = f"https://get.helm.sh/helm-{_HELM_VERSION}-linux-{_HELM_ARCHITECTURE}.tar.gz"


def _ensure_helm_binary() -> Path:
    """Download Helm on the host (k3s busybox wget has no HTTPS) and stage it for Kathara."""
    cache_dir = RUNTIME_DIR / ".nika_cache" / "helm" / _HELM_VERSION
    helm_bin = cache_dir / f"helm-{_HELM_ARCHITECTURE}"
    if helm_bin.is_file() and os.access(helm_bin, os.X_OK):
        return helm_bin

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "helm.tgz"
        urllib.request.urlretrieve(_HELM_ARCHIVE_URL, archive)
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(f"linux-{_HELM_ARCHITECTURE}/helm")
            tar.extract(member, path=tmp, filter="data")
        extracted = Path(tmp) / f"linux-{_HELM_ARCHITECTURE}" / "helm"
        shutil.copy2(extracted, helm_bin)
        helm_bin.chmod(0o755)
    return helm_bin


class LLMDInferenceCluster(NetworkEnvBase):
    LAB_NAME = "llmd_lab"
    VERIFY_MAX_WAIT_SEC = 1800
    VERIFY_RETRY_DELAY_SEC = 20
    TOPO_LEVEL = "hard"
    TOPO_SIZE = None
    TAGS = [
        "kubernetes",
        "k3s",
        "k8s_control_plane",
        "metallb",
        "coredns",
        "kube_proxy",
        "llm",
        "inference",
        "link",
        "pc",
        "http",
        "icmp",
        "arp",
        "mac",
    ]

    def __init__(self, **kwargs):
        super().__init__()
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A star-topology Kubernetes (k3s) cluster running llm-d with disaggregated "
            "Prefill/Decode inference. All nodes are bridged for internet access to pull "
            "container images. Uses Gateway API and inference extensions."
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

        for name, (links, is_controller) in _k3s_machines.items():
            m = self.lab.new_machine(name, **{"image": _K3S_IMAGE})
            m.add_meta("privileged", True)
            m.add_meta("bridged", True)
            for ulimit in _K3S_ULIMITS:
                m.add_meta("ulimit", ulimit)
            m.add_meta("shell", "/bin/sh")
            if is_controller:
                m.add_meta("env", "K3S_TOKEN=secret")
                m.add_meta("env", "VERIFY_CHECKSUM=false")
                m.add_meta("env", "KUBECONFIG=/etc/rancher/k3s/k3s.yaml")
                m.add_meta(
                    "args",
                    "server --disable servicelb --disable traefik --write-kubeconfig-mode 644",
                )
            else:
                m.add_meta("env", "K3S_URL=https://controller:6443")
                m.add_meta("env", "K3S_TOKEN=secret")
            for link in links:
                self.lab.connect_machine_to_link(name, link)
            all_machines[name] = m

        # Client machine for testing service reachability
        client = self.lab.new_machine("client", **{"image": _BASE_IMAGE})
        self.lab.connect_machine_to_link("client", "A")
        all_machines["client"] = client

        # Stage Helm into controller FS (busybox wget cannot fetch HTTPS).
        helm_bin = _ensure_helm_binary()
        helm_stage = Path(cur_path) / "controller" / "usr" / "local" / "bin"
        helm_stage.mkdir(parents=True, exist_ok=True)
        staged_helm = helm_stage / "helm"
        if not staged_helm.is_file():
            shutil.copy2(helm_bin, staged_helm)
            staged_helm.chmod(0o755)

        # Load per-machine configuration directories and startup scripts
        for name, m in all_machines.items():
            machine_dir = os.path.join(cur_path, name)
            if os.path.isdir(machine_dir):
                m.copy_directory_from_path(machine_dir, "/")
            startup_file = os.path.join(cur_path, f"{name}.startup")
            if os.path.isfile(startup_file):
                self.lab.create_file_from_path(startup_file, f"{name}.startup")

        self.load_machines()

    def load_machines(self):
        super().load_machines()
        self.kubernetes_nodes = sorted(name for name, m in self.lab.machines.items() if "k3s" in m.get_image())

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.kubernetes.llmd_lab.verify import verify_llmd_lab

        return verify_llmd_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
