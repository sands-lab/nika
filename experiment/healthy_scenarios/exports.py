"""Read native forwarding state by declared device capabilities."""

from __future__ import annotations


def export_script(capabilities) -> tuple[str, str]:
    caps = set(capabilities)
    if "routeros" in caps:
        from nika.service.lab.routeros_api import CLI_COMMAND

        return "routeros", "\n" + "\n".join(
            f"echo '@@@ {label}'\n" + CLI_COMMAND.format(command=command)
            for label, command in [
                ("routeros_routes", "/ip route print terse without-paging"),
                ("routeros_bgp", "/routing bgp session print detail without-paging"),
                ("routeros_addresses", "/ip address print detail without-paging"),
                ("routeros_arp", "/ip arp print detail without-paging"),
            ]
        ) + "\n"
    if "iosxr" in caps:
        from nika.service.lab.iosxr_api import CLI_COMMAND

        return "iosxr", "\n" + "\n".join(
            f"echo '@@@ {label}'\n" + CLI_COMMAND.format(command=command)
            for label, command in [
                ("iosxr_fib", "show cef ipv4 detail"),
                ("iosxr_rib", "show route ipv4 unicast detail"),
                ("iosxr_bgp", "show bgp summary"),
                ("iosxr_interfaces", "show ipv4 interface brief"),
            ]
        ) + "\n"
    if "nokia_srlinux" in caps or "srlinux" in caps:
        return "srlinux", ""
    if "sonic" in caps:
        return "sonic", "\necho '@@@ sonic_rib'\nvtysh -c 'show ip route json'\n"
    if "ovs" in caps:
        return (
            "openflow",
            '\nfor bridge in $(ovs-vsctl list-br); do\necho "@@@ ovs_flows:$bridge"\novs-ofctl -O OpenFlow13 dump-flows "$bridge"\novs-ofctl -O OpenFlow13 dump-groups "$bridge"\ndone\n',
        )
    if "p4runtime" in caps:
        if "bmv2" not in caps:
            from nika.service.kathara.bmv2_api import p4rt_command

            return "p4runtime", "\necho '@@@ p4runtime'\n" + p4rt_command("read") + "\n"
        return "p4runtime", ""
    return "linux", (
        "\necho '@@@ kernel_release'\nuname -r\n"
        "echo '@@@ boot_id'\ncat /proc/sys/kernel/random/boot_id\n"
        "echo '@@@ ecmp_hash_policy'\ncat /proc/sys/net/ipv4/fib_multipath_hash_policy\n"
        "echo '@@@ ecmp_hash_seed'\ncat /proc/sys/net/ipv4/fib_multipath_hash_seed\n"
    )
