"""Syslog telemetry: RFC3164/RFC5424 UDP to the shared collector.

A telemetry kind for ``nika.telemetry.management``. Linux devices forward
``/dev/log`` (FRR ``log syslog``, OVS syslog) over the management VRF. SR Linux
uses gNMI remote-server from the mgmt network-instance. XRd uses the same Linux
forwarder because extra Kathara NICs stay on Linux. RouterOS adds a remote
logging action on the extra ether.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nika.net_env.verify import exec_or_empty, process_running
from nika.telemetry import CONFIG_DIR, DEVICE_EXEC_WORKERS, MGMT_VRF
from nika.utils.parallel import bounded_parallel_map

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

SYSLOG_PORT = 514
SYSLOG_DIR = "/var/log/remote"
SYSLOG_HOSTS = f"{CONFIG_DIR}/syslog_hosts"
# Distro AppArmor profiles allow this path; /etc/telemetry/rsyslog.conf is denied.
SYSLOG_CONFIG = "/etc/rsyslog.conf"
SYSLOG_FORWARDER = f"{CONFIG_DIR}/syslog_forward.py"
UNIX_VERSION = "unix"
# Drop collector internals and benchmark identifiers; keep topology names
# that only contain the same letters as a substring.
_INTERNAL = re.compile(
    r"telemetry_collector|telemetry_switch|nika_telemetry_|(?<![a-z])nika(?![a-z])",
    re.I,
)
_LINE = re.compile(
    r"^(?P<time>\S+)\s+(?P<host>\S+)\s+(?P<program>[^:]+:?)\s*(?P<message>.*)$"
)


def is_unix(version: str) -> bool:
    return version == UNIX_VERSION


def parse_log_line(line: str) -> dict[str, str] | None:
    """Parse one collector line into time, host, program, and message."""
    text = line.strip()
    if not text:
        return None
    match = _LINE.match(text)
    if not match:
        return None
    program = match.group("program").rstrip(":").split("[", 1)[0].strip()
    message = match.group("message").strip()
    blob = f"{match.group('host')} {program} {message}"
    if _INTERNAL.search(blob):
        return None
    return {
        "time": match.group("time"),
        "host": match.group("host"),
        "program": program,
        "message": message,
    }


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return a version when the node can export syslog to the collector."""
    from nika.telemetry import iosxr, routeros, srl

    vendor = (
        srl.probe(runtime, name)
        or iosxr.probe(runtime, name)
        or routeros.probe(runtime, name)
    )
    if vendor:
        return vendor
    out = exec_or_empty(runtime, name, "command -v python3")
    return UNIX_VERSION if "python3" in out else None


def device_files(
    device: dict[str, Any],
    address: str,
    collector: str,
    version: str = "",
) -> dict[str, str]:
    from nika.telemetry import iosxr, routeros, srl

    if srl.is_srl(version):
        return {}
    if iosxr.is_xr(version):
        # Extra Kathara NICs stay on Linux; XR has no path to the collector.
        return {SYSLOG_FORWARDER: _forwarder_script(address, MGMT_VRF, collector)}
    if routeros.is_ros(version):
        return {routeros.SYSLOG_SCRIPT: routeros.syslog_script(address, collector)}
    return {SYSLOG_FORWARDER: _forwarder_script(address, MGMT_VRF, collector)}


def device_lines(device: dict[str, Any], address: str, version: str = "") -> list[str]:
    from nika.telemetry import iosxr, routeros, srl

    del device, address
    if srl.is_srl(version):
        return []
    if iosxr.is_xr(version):
        return [f"(while true; do python3 {SYSLOG_FORWARDER}; sleep 1; done) &"]
    if routeros.is_ros(version):
        return [f"sh {routeros.SYSLOG_SCRIPT}"]
    return [
        f"(while true; do python3 {SYSLOG_FORWARDER}; sleep 1; done) &",
        "sleep 1",
        'vtysh -c "configure terminal" -c "log syslog informational" '
        ">/dev/null 2>&1 || true",
        "ovs-appctl vlog/set syslog:info >/dev/null 2>&1 || true",
    ]


def collector_files(
    targets: dict[str, str], versions: dict[str, str] | None = None
) -> dict[str, str]:
    del versions
    return {
        SYSLOG_HOSTS: "".join(f"{ip} {name}\n" for name, ip in targets.items()),
        SYSLOG_CONFIG: _rsyslog_config(),
    }


def collector_lines(
    targets: dict[str, str] | None = None, versions: dict[str, str] | None = None
) -> list[str]:
    del targets, versions
    return [
        f"mkdir -p {SYSLOG_DIR} /var/lib/rsyslog",
        f"(while true; do rsyslogd -n -i /run/rsyslogd.pid -f {SYSLOG_CONFIG}; "
        "sleep 1; done) &",
    ]


def _exporter_running(runtime: LabRuntime, name: str) -> bool:
    from nika.telemetry import routeros, srl

    if exec_or_empty(runtime, name, f"pgrep -f {SYSLOG_FORWARDER}").strip():
        return True
    if srl.syslog_enabled(runtime, name):
        return True
    return routeros.syslog_enabled(runtime, name)


def running(runtime: LabRuntime, collector: str, targets: dict[str, str]) -> bool:
    """rsyslogd is up and every configured device is exporting."""
    if not process_running(runtime, collector, "rsyslogd"):
        return False
    if not targets:
        return True
    return all(
        bounded_parallel_map(
            lambda name: _exporter_running(runtime, name),
            sorted(targets),
            max_workers=DEVICE_EXEC_WORKERS,
        )
    )


def _rsyslog_config() -> str:
    return f"""\
module(load="imudp")
input(type="imudp" port="{SYSLOG_PORT}")
template(name="RemoteFile" type="string" string="{SYSLOG_DIR}/%fromhost-ip%.log")
template(name="RemoteFmt" type="string"
  string="%timegenerated:::date-rfc3339% %fromhost-ip% %syslogtag%%msg%\\n")
if $fromhost-ip != "127.0.0.1" then {{
  action(type="omfile" dynaFile="RemoteFile" template="RemoteFmt")
  stop
}}
"""


def _forwarder_script(bind: str, device: str, dest: str) -> str:
    return f"""\
#!/usr/bin/env python3
import os
import socket
import time

BIND = {bind!r}
DEVICE = {device!r}
DEST = ({dest!r}, {SYSLOG_PORT})
PATH = "/dev/log"


def main() -> int:
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, DEVICE.encode() + b"\\0")
    out.bind((BIND, 0))
    try:
        os.unlink(PATH)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(PATH)
    os.chmod(PATH, 0o666)
    host = socket.gethostname().split(".")[0]
    while True:
        payload, _ = server.recvfrom(8192)
        if not payload:
            continue
        if payload.startswith(b"<"):
            out.sendto(payload, DEST)
            continue
        stamp = time.strftime("%b %d %H:%M:%S").encode()
        out.sendto(b"<30>" + stamp + b" " + host.encode() + b" forwarder: " + payload, DEST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
