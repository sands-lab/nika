"""Fixed catalog of read-only state views the recorder takes on every node.

A view applies to a node through the node's declared capabilities or the
tools installed in its image, never through the scenario or the injected
failure. Every task of an environment therefore records the same files, and
only their contents differ.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Tools whose presence selects views. Probed once per node with ``command -v``.
DETECTED_TOOLS = (
    "vtysh",
    "nft",
    "iptables-save",
    "tc",
    "bridge",
    "ss",
    "nstat",
    "wg",
    "ovs-vsctl",
    "kubectl",
    "conntrack",
    "nginx",
    "named-checkconf",
    "dhcpd",
    "simple_switch",
    "simple_switch_grpc",
    "sr_cli",
    "openssl",
)

DETECT_COMMAND = (
    "for c in "
    + " ".join(DETECTED_TOOLS)
    + "; do command -v $c >/dev/null 2>&1 && echo $c; done; true"
)


@dataclass(frozen=True)
class NodeFacts:
    name: str
    role: str
    capabilities: frozenset[str]
    tools: frozenset[str]


@dataclass(frozen=True)
class View:
    """One snapshot file: ``nodes/<node>/<name>.txt``."""

    name: str
    command: str
    applies: Callable[[NodeFacts], bool]
    timeout: float = 30


def _tool(*names: str) -> Callable[[NodeFacts], bool]:
    return lambda n: any(t in n.tools for t in names)


def _cap(*names: str) -> Callable[[NodeFacts], bool]:
    return lambda n: any(c in n.capabilities for c in names)


def _shell(n: NodeFacts) -> bool:
    # SR Linux and SONiC containers have a Linux shell too; Linux views on them
    # show the host namespace, which is still uniform across tasks.
    return True


def _section(title: str, command: str) -> str:
    return f"echo '### {title}'; {command} 2>&1; echo"


def _sections(*items: tuple[str, str]) -> str:
    return "; ".join(_section(title, cmd) for title, cmd in items)


_PER_DEV_TC = (
    'for d in $(ls /sys/class/net); do echo "### dev $d"; '
    "tc -s qdisc show dev $d 2>&1; tc -s class show dev $d 2>&1; "
    "echo '# ingress filters'; tc -s filter show dev $d ingress 2>&1; "
    "echo '# egress filters'; tc -s filter show dev $d egress 2>&1; echo; done"
)

# Marks every recorder command so process listings can leave them out.
RECORDER_MARK = ": offline-recorder;"

# Log files under /var/log and /tmp. Left out: the monitoring plane's own logs,
# image-build package logs, and the deploy script's startup log, which would
# hand the model the pristine configuration to diff against.
_LOG_FILES = (
    "for f in $(ls /var/log/*.log /var/log/*/*.log /var/log/syslog /var/log/messages "
    "/tmp/*.log 2>/dev/null | grep -v -e '/var/log/telemetry' -e '/var/log/remote/' "
    "-e '/var/log/apt/' -e '/var/log/dpkg.log' -e '/var/log/alternatives.log' "
    "-e '/var/log/startup.log' | sort -u); "
    'do [ -f "$f" ] && { echo "### $f"; tail -n 200 "$f"; echo; }; done; true'
)

_APP_FILES = (
    "for f in /etc/dhcp/dhcpd.conf /var/lib/dhcp/*.leases /var/lib/dhcpd/*.leases "
    "/etc/bind/named.conf* /etc/bind/db.* /etc/bind/*.zone /etc/bind/zones/* "
    "/var/lib/bind/* /var/cache/bind/*.zone /etc/nginx/nginx.conf /etc/nginx/conf.d/* "
    "/etc/nginx/sites-enabled/* /etc/lb-agent/*.json /var/lib/lb-agent/*.json "
    "/etc/wireguard/*.conf /run/flannel/subnet.env; "
    'do [ -f "$f" ] && { echo "### $f"; head -c 20000 "$f"; echo; }; done; true'
)

_TLS_CERTS = (
    "for c in $(nginx -T 2>/dev/null | awk '$1==\"ssl_certificate\"{print $2}' | tr -d ';' | sort -u); "
    'do echo "### $c"; openssl x509 -in "$c" -noout -subject -issuer -dates -serial 2>&1; done; true'
)

_OVS = (
    "ovs-vsctl show 2>&1; echo; "
    "ovs-vsctl --columns=_uuid,target,is_connected,role list controller 2>&1; echo; "
    'for b in $(ovs-vsctl list-br); do echo "### bridge $b"; '
    'echo "datapath_id=$(ovs-vsctl get bridge $b datapath_id)"; '
    'echo "other_config=$(ovs-vsctl get bridge $b other-config)"; '
    "echo '# ports-desc'; ovs-ofctl -O OpenFlow13 dump-ports-desc $b 2>&1; "
    "echo '# flows'; ovs-ofctl -O OpenFlow13 dump-flows $b 2>&1; "
    "echo '# groups'; ovs-ofctl -O OpenFlow13 dump-groups $b 2>&1; "
    "echo '# port stats'; ovs-ofctl -O OpenFlow13 dump-ports $b 2>&1; echo; done; true"
)

_K8S_KINDS = (
    "nodes,pods,services,endpoints,endpointslices,deployments,daemonsets,"
    "replicasets,configmaps,ingresses,networkpolicies"
)
_K8S_CRDS = (
    "kubectl api-resources --verbs=list -o name 2>/dev/null | "
    "grep -E 'gateway|httproute|inferencepool|metallb|ipaddresspool|advertisement|bgppeer' "
    "| paste -sd, -"
)
_K8S_LOGS = (
    "kubectl get pods -A --no-headers -o custom-columns=NS:.metadata.namespace,N:.metadata.name 2>/dev/null "
    '| while read ns p; do echo "### $ns/$p"; '
    "kubectl logs -n $ns $p --all-containers --prefix --tail=80 2>&1; "
    "echo '# previous'; kubectl logs -n $ns $p --all-containers --prefix --tail=40 --previous 2>&1 | head -40; "
    "echo; done; true"
)

_VTYSH = "vtysh -c"


def _vtysh(*commands: str) -> str:
    return "; ".join(f"echo '### {c}'; {_VTYSH} '{c}' 2>&1; echo" for c in commands)


VIEWS: tuple[View, ...] = (
    # -- every node -----------------------------------------------------------
    View(
        "system",
        _sections(
            ("clock", "date -u +%Y-%m-%dT%H:%M:%SZ"),
            ("uptime", "uptime"),
            ("cgroup cpu", "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat"),
        ),
        _shell,
    ),
    View(
        "interfaces",
        _sections(
            ("ip -br link", "ip -br link"),
            ("ip -d addr", "ip -d addr show"),
            ("ip -s -s link", "ip -s -s link show"),
        ),
        _shell,
    ),
    View(
        "routes",
        _sections(
            ("ip -4 route show table all", "ip -4 route show table all"),
            ("ip -6 route show table all", "ip -6 route show table all"),
            ("ip rule", "ip rule show"),
        ),
        _shell,
    ),
    View(
        "neighbors",
        _sections(
            ("ip neigh", "ip neigh show"), ("/proc/net/arp", "cat /proc/net/arp")
        ),
        _shell,
    ),
    View(
        "bridge",
        _sections(
            ("bridge link", "bridge link show"),
            ("bridge fdb", "bridge fdb show"),
            ("bridge vlan", "bridge vlan show"),
        ),
        _tool("bridge"),
    ),
    View(
        "firewall",
        _sections(("nft list ruleset", "nft list ruleset"))
        + "; "
        + _sections(("iptables-save", "iptables-save")),
        _tool("nft", "iptables-save"),
    ),
    View("tc", _PER_DEV_TC, _tool("tc"), timeout=45),
    View(
        "sockets",
        _sections(
            ("ss -s", "ss -s"),
            ("listening", "ss -lntup"),
            (
                "tcp states",
                "ss -tan | awk 'NR>1{print $1}' | sort | uniq -c | sort -rn",
            ),
            ("tcp sockets (first 200)", "ss -tanoi | head -400"),
        ),
        _tool("ss"),
    ),
    View(
        "netstat_counters",
        "nstat -asz 2>/dev/null || cat /proc/net/snmp /proc/net/netstat",
        _shell,
    ),
    View(
        "sysctl",
        "sysctl net.ipv4.ip_forward net.ipv4.conf net.ipv4.tcp_rmem net.ipv4.tcp_wmem "
        "net.ipv4.tcp_moderate_rcvbuf net.ipv4.tcp_syncookies net.ipv4.tcp_max_syn_backlog "
        "net.ipv4.ip_local_port_range net.core.rmem_max net.core.wmem_max "
        "net.core.somaxconn net.ipv6.conf.all.disable_ipv6 2>&1 | grep -v 'lo\\.'",
        _shell,
    ),
    View(
        "processes",
        "ps -eo pid,ppid,stat,pcpu,pmem,etime,args --sort=-pcpu 2>/dev/null "
        "| grep -v -e 'offline-recorder' -e 'runc init' -e 'python3 -c \"\"\"' -e ' ps -eo ' "
        "| cut -c1-240 | head -80",
        _shell,
    ),
    View(
        "resolver",
        _sections(
            ("/etc/resolv.conf", "cat /etc/resolv.conf"),
            ("/etc/hosts", "cat /etc/hosts"),
        ),
        _shell,
    ),
    View(
        "conntrack",
        _sections(
            ("conntrack -S", "conntrack -S"),
            ("conntrack -C", "conntrack -C"),
            ("conntrack -L (first 300)", "conntrack -L 2>/dev/null | head -300"),
        ),
        _tool("conntrack"),
    ),
    View("logs", _LOG_FILES, _shell),
    View("app_config", _APP_FILES, _shell),
    # -- routing --------------------------------------------------------------
    View("frr_config", _vtysh("show running-config"), _tool("vtysh")),
    View(
        "frr_rib",
        _vtysh(
            "show interface brief", "show ip route vrf all", "show ipv6 route vrf all"
        ),
        _tool("vtysh"),
    ),
    View(
        "frr_bgp",
        _vtysh(
            "show bgp vrf all summary",
            "show bgp vrf all ipv4 unicast",
            "show bgp vrf all neighbors",
        ),
        _tool("vtysh"),
        timeout=45,
    ),
    View(
        "frr_igp",
        _vtysh(
            "show ip ospf",
            "show ip ospf neighbor",
            "show ip ospf interface",
            "show ip ospf database",
            "show isis neighbor",
            "show isis interface",
            "show isis database",
        ),
        _tool("vtysh"),
        timeout=45,
    ),
    View(
        "frr_rpki",
        _vtysh("show rpki cache-connection", "show rpki prefix-table"),
        _cap("rpki", "rov", "rtr"),
    ),
    View(
        "srl",
        "; ".join(
            f"echo '### {c}'; sr_cli '{c}' 2>&1; echo"
            for c in (
                "show interface",
                "show network-instance default route-table",
                "show network-instance default protocols bgp neighbor",
                "show network-instance default protocols isis adjacency",
                "show acl",
                "info from running /network-instance default protocols",
            )
        ),
        _tool("sr_cli"),
        timeout=60,
    ),
    # -- tunnels, services ----------------------------------------------------
    View("wireguard", "wg show all 2>&1", _tool("wg")),
    View("nginx", "nginx -T 2>&1 | head -400", _tool("nginx")),
    View("tls_certificates", _TLS_CERTS, lambda n: {"nginx", "openssl"} <= n.tools),
    # -- programmable data planes ---------------------------------------------
    View("ovs", _OVS, _tool("ovs-vsctl"), timeout=45),
    View(
        "bmv2_process",
        "pgrep -af '[s]imple_switch' || echo NO_SIMPLE_SWITCH_PROCESS",
        _cap("bmv2"),
    ),
    # -- Kubernetes (on the node that holds kubectl credentials) --------------
    View(
        "k8s_objects",
        f"kubectl get {_K8S_KINDS} -A -o wide 2>&1; echo; "
        f'crd=$({_K8S_CRDS}); [ -n "$crd" ] && kubectl get $crd -A -o wide 2>&1; true',
        lambda n: (
            "kubectl" in n.tools and "k3s" in n.capabilities and n.role == "controller"
        ),
        timeout=60,
    ),
    View(
        "k8s_objects_yaml",
        f"kubectl get {_K8S_KINDS} -A -o yaml 2>&1; "
        f'crd=$({_K8S_CRDS}); [ -n "$crd" ] && kubectl get $crd -A -o yaml 2>&1; true',
        lambda n: (
            "kubectl" in n.tools and "k3s" in n.capabilities and n.role == "controller"
        ),
        timeout=90,
    ),
    View(
        "k8s_events",
        "kubectl get events -A --sort-by=.lastTimestamp -o wide 2>&1 | tail -300",
        lambda n: (
            "kubectl" in n.tools and "k3s" in n.capabilities and n.role == "controller"
        ),
        timeout=60,
    ),
    View(
        "k8s_pod_logs",
        _K8S_LOGS,
        lambda n: (
            "kubectl" in n.tools and "k3s" in n.capabilities and n.role == "controller"
        ),
        timeout=180,
    ),
)


def views_for(node: NodeFacts) -> list[View]:
    return [view for view in VIEWS if view.applies(node)]
