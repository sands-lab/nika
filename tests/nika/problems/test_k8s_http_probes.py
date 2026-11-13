"""Service-entry probes reuse the scenario's declared HTTP request."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.net_env.llmd_lab.lab import (
    INFERENCE_CHAT_BODY,
    INFERENCE_CHAT_URL,
    LLMDInferenceCluster,
)
from nika.net_env.k8s_lab.lab import K8sFatTreeBGP
from nika.validation.symptom.k8s_probes import _backend_request
from tests.support.net_env import instantiate_with_mocked_kathara

pytestmark = pytest.mark.unit


def test_weather_backend_request_reaches_the_database():
    env = instantiate_with_mocked_kathara(
        "nika.net_env.k8s_lab.lab.Kathara.get_instance",
        K8sFatTreeBGP,
    )
    problem = SimpleNamespace(net_env=env)
    req = _backend_request(problem, "http://datacenter.com/weather")
    assert req.url == "http://datacenter.com/weather?location=London"
    assert _backend_request(problem, "http://datacenter.com/word").url.endswith("/word")
    assert _backend_request(problem, req.url).url == req.url


def test_llmd_backend_request_is_chat_completions() -> None:
    env = instantiate_with_mocked_kathara(
        "nika.net_env.llmd_lab.lab.Kathara.get_instance",
        LLMDInferenceCluster,
    )
    req = _backend_request(SimpleNamespace(net_env=env), "http://200.0.0.240/")
    assert req.method == "POST"
    assert req.url == INFERENCE_CHAT_URL
    assert req.body == INFERENCE_CHAT_BODY
    assert req.succeeded("200", '{"choices":[{"message":{"content":"ok"}}]}')
    assert not req.succeeded("404", "Not Found")
    assert not req.succeeded("200", "{}")
    assert not req.succeeded("000", "")


def test_backend_request_defaults_to_get() -> None:
    req = _backend_request(
        SimpleNamespace(net_env=SimpleNamespace()),
        "http://datacenter.com/word",
    )
    assert req.method == "GET"
    assert req.url == "http://datacenter.com/word"
    assert req.body is None
    assert req.succeeded("200", "")
    assert req.succeeded("404", "")
    assert not req.succeeded("500", "")
    assert not req.succeeded("000", "")
