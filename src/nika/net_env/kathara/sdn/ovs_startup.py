"""Shared Open vSwitch startup for Kathara SDN labs.

Host kernels often advertise many CPUs while each switch container is capped
at a few hundred MiB. Default ovs-vswitchd sizing (~1.25×ncpu upcall threads)
then aborts during startup; ``ovs-vsctl`` hangs and ports never attach.
"""

from __future__ import annotations

# Keep thread counts tiny so 256–512MiB switch containers stay alive.
_OVS_HANDLER_THREADS = 2
_OVS_REVALIDATOR_THREADS = 1


def ovs_start_commands() -> list[str]:
    """Return shell commands that start OVS with bounded upcall threads."""
    return [
        "mkdir -p /etc/openvswitch /var/run/openvswitch /var/log/openvswitch",
        "ovsdb-tool create /etc/openvswitch/conf.db "
        "/usr/share/openvswitch/vswitch.ovsschema 2>/dev/null || true",
        "ovsdb-server --remote=punix:/var/run/openvswitch/db.sock "
        "--pidfile=/var/run/openvswitch/ovsdb-server.pid "
        "--detach --log-file=/var/log/openvswitch/ovsdb-server.log",
        "ovs-vsctl --no-wait --timeout=10 init",
        "ovs-vsctl --no-wait --timeout=10 set Open_vSwitch . "
        f"other_config:n-handler-threads={_OVS_HANDLER_THREADS} "
        f"other_config:n-revalidator-threads={_OVS_REVALIDATOR_THREADS}",
        "ovs-vswitchd --pidfile=/var/run/openvswitch/ovs-vswitchd.pid "
        "--detach --log-file=/var/log/openvswitch/ovs-vswitchd.log",
    ]
