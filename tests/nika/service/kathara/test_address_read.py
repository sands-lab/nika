"""Address discovery distinguishes an empty exec response from an unaddressed host."""

import json
from unittest.mock import Mock

import pytest

from nika.service.kathara.base_api import KatharaBaseAPI

pytestmark = pytest.mark.unit


def address_api(*responses):
    api = object.__new__(KatharaBaseAPI)
    api.exec_cmd_checked = Mock(side_effect=responses)
    return api


@pytest.mark.parametrize("empty", ["", " \n", []])
def test_empty_exec_response_is_retried_before_address_discovery(empty):
    addresses = json.dumps(
        [{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "10.0.0.2"}]}]
    )
    api = address_api(empty, addresses)
    assert api.get_host_ip("pc1") == "10.0.0.2"
    assert api.exec_cmd_checked.call_count == 2


def test_persistently_empty_response_remains_a_discovery_failure():
    api = address_api("", "")
    with pytest.raises(RuntimeError, match="Failed to parse"):
        api.get_host_ip("pc1")
    assert api.exec_cmd_checked.call_count == 2


@pytest.mark.parametrize("response", ["[]", '[{"ifname":"eth0","addr_info":[]}]'])
def test_host_without_an_address_does_not_acquire_a_cached_or_expected_address(
    response,
):
    api = address_api(response)
    assert api.get_host_ip("pc1") is None
    assert api.exec_cmd_checked.call_count == 1


def test_nonempty_exec_error_remains_a_discovery_failure():
    api = address_api("Error executing command on pc1")
    with pytest.raises(RuntimeError, match="Failed to parse"):
        api.get_host_ip("pc1")
    assert api.exec_cmd_checked.call_count == 1
