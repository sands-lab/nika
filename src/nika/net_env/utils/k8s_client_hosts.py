"""Sync client /etc/hosts with Kubernetes MetalLB VIPs for k8s_lab and llmd_lab."""

from __future__ import annotations

from nika.net_env.verify import exec_or_empty
from nika.runtime.base import LabRuntime


def models_http_url(gateway_addr: str) -> str:
    if gateway_addr.startswith("200.0.0."):
        return f"http://{gateway_addr}/v1/models"
    return "http://llmd/v1/models"


def word_app_http_url(ingress_ip: str) -> str:
    # Ingress rules match host datacenter.com; curl by VIP IP alone returns 404.
    del ingress_ip  # VIP is synced into client /etc/hosts before probing.
    return "http://datacenter.com/word"


def weather_app_http_url(ingress_ip: str) -> str:
    del ingress_ip
    # App returns non-200 without a location query (see k8s_lab verify E2E).
    return "http://datacenter.com/weather?location=London"


def sync_llmd_client_hosts(runtime: LabRuntime) -> str | None:
    """Point client ``llmd`` at the live Gateway VIP; return the VIP when updated."""
    gateway_addr = exec_or_empty(
        runtime,
        "controller",
        "kubectl get gateway -n llm-d llm-d-gateway "
        "-o jsonpath={.status.addresses[0].value}",
        timeout=60,
    ).strip()
    if not gateway_addr.startswith("200.0.0."):
        return None
    runtime.exec(
        "client",
        "grep -v '[[:space:]]llmd$' /etc/hosts > /tmp/hosts.nika "
        f"&& echo '{gateway_addr} llmd' >> /tmp/hosts.nika "
        "&& cat /tmp/hosts.nika > /etc/hosts",
        timeout=15.0,
    )
    return gateway_addr


def sync_k8s_client_hosts(runtime: LabRuntime) -> str | None:
    """Point client ``datacenter.com`` at the ingress VIP; return the VIP when updated."""
    ingress_ip = exec_or_empty(
        runtime,
        "controller",
        "kubectl get svc -n ingress-nginx ingress-nginx-controller "
        "-o jsonpath={.status.loadBalancer.ingress[0].ip}",
        timeout=60,
    ).strip()
    if not ingress_ip.startswith("101."):
        return None
    runtime.exec(
        "client",
        "grep -v datacenter.com /etc/hosts > /tmp/hosts.nika "
        f"&& echo '{ingress_ip} datacenter.com' >> /tmp/hosts.nika "
        "&& cat /tmp/hosts.nika > /etc/hosts",
        timeout=15.0,
    )
    return ingress_ip
