"""Unit tests for Kubernetes client hosts sync helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nika.net_env.utils.k8s_client_hosts import (
    models_http_url,
    sync_k8s_client_hosts,
    sync_llmd_client_hosts,
    word_app_http_url,
)


def test_models_http_url_uses_gateway_vip_when_present() -> None:
    assert models_http_url("200.0.0.241") == "http://200.0.0.241/v1/models"
    assert models_http_url("200.0.0.240") == "http://200.0.0.240/v1/models"


def test_models_http_url_falls_back_to_hostname() -> None:
    assert models_http_url("") == "http://llmd/v1/models"
    assert models_http_url("pending") == "http://llmd/v1/models"


def test_word_app_http_url_uses_hostname_after_ingress_vip() -> None:
    assert word_app_http_url("101.0.0.42") == "http://datacenter.com/word"


def test_word_app_http_url_falls_back_to_hostname() -> None:
    assert word_app_http_url("") == "http://datacenter.com/word"


def test_sync_llmd_client_hosts_updates_client_etc_hosts() -> None:
    runtime = MagicMock()
    with patch(
        "nika.net_env.utils.k8s_client_hosts.exec_or_empty",
        return_value="200.0.0.241",
    ):
        vip = sync_llmd_client_hosts(runtime)

    assert vip == "200.0.0.241"
    runtime.exec.assert_called_once()
    assert "200.0.0.241 llmd" in runtime.exec.call_args.args[1]


def test_sync_k8s_client_hosts_updates_client_etc_hosts() -> None:
    runtime = MagicMock()
    with patch(
        "nika.net_env.utils.k8s_client_hosts.exec_or_empty",
        return_value="101.0.0.42",
    ):
        vip = sync_k8s_client_hosts(runtime)

    assert vip == "101.0.0.42"
    runtime.exec.assert_called_once()
    assert "101.0.0.42 datacenter.com" in runtime.exec.call_args.args[1]
