"""Vendor captures retain native names, bytes and explicit read errors."""

from types import SimpleNamespace

import pytest

from nika.service.packet_capture.capture_backend import capture_device

pytestmark = pytest.mark.unit


class Runtime:
    backend = "kathara"

    def __init__(self, capability="iosxr", present=True):
        self._net_env = SimpleNamespace(
            machine_identities={"spine": SimpleNamespace(capabilities={capability})}
        )
        self.present = present
        self.commands = []

    def exec(self, device, command, **kwargs):
        self.commands.append(command)
        if command.startswith("ls "):
            return "eth0 eth1 lo"
        return "" if self.present else "missing"


@pytest.mark.parametrize(
    "interface,linux",
    [
        ("GigabitEthernet0/0/0/0", "eth0"),
        ("Gi0/0/0/1", "eth1"),
        ("eth0", "eth0"),
        ("any", "any"),
    ],
)
def test_capture_resolves_native_xr_data_ports(interface, linux):
    runtime = Runtime()
    assert capture_device(runtime, "spine", interface) == linux
    if linux != "any":
        assert f"/sys/class/net/{linux}" in runtime.commands[0]


def test_capture_rejects_a_missing_xr_port_after_translation():
    runtime = Runtime(present=False)
    with pytest.raises(ValueError, match="GigabitEthernet0/0/0/99"):
        capture_device(runtime, "spine", "GigabitEthernet0/0/0/99")
    assert "/sys/class/net/eth99" in runtime.commands[0]


def test_native_xr_names_do_not_remap_other_capabilities():
    runtime = Runtime(capability="frr", present=False)
    with pytest.raises(ValueError, match="does not exist"):
        capture_device(runtime, "spine", "Gi0/0/0/0")
    assert "/sys/class/net/Gi0/0/0/0" in runtime.commands[0]
