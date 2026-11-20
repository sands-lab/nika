"""Context discovery for CLI clients behind an authenticated model proxy."""

import io
import json
from urllib.error import URLError

import pytest

from agent.cli.model_metadata import model_context_window

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("base", ["http://proxy", "http://proxy/v1/"])
def test_reads_selected_model_context_with_proxy_credential(monkeypatch, base):
    def respond(request, **kwargs):
        assert request.full_url == "http://proxy/v1/models"
        assert request.get_header("Authorization") == "Bearer placeholder"
        return io.StringIO(
            json.dumps(
                {
                    "data": [
                        {"id": "other", "max_model_len": 128000},
                        {"id": "selected", "max_model_len": 1000000},
                    ]
                }
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", respond)
    assert model_context_window(base, "selected", "placeholder") == 1_000_000


@pytest.mark.parametrize("value", [None, True, 0, -1, "1000000"])
def test_unknown_context_is_not_invented(monkeypatch, value):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: io.StringIO(
            json.dumps({"data": [{"id": "m", "max_model_len": value}]})
        ),
    )
    assert model_context_window("http://proxy/v1", "m", "") is None


def test_unavailable_metadata_keeps_context_unknown(monkeypatch):
    def unavailable(*args, **kwargs):
        raise URLError("not ready")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    assert model_context_window("http://proxy", "m", "") is None
