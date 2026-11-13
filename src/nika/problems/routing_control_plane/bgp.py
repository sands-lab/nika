import ipaddress
import json
import random
import re
import shlex

from pydantic import BaseModel, Field

from nika.net_env.isp.bgp.frr import EXPORT_DENY_PREFIX_LIST
from nika.net_env.isp.identity import list_isp_base_scenarios
from nika.problems.routing_control_plane.ospf import linked_routers
from nika.problems.support.frr import (
    FRR_SAVE_COMMAND,
    frr_config_command,
    frr_save_config,
)
from nika.problems.support.inject_resolve import resolve_victim_host_ip
from nika.problems.support.kubernetes.base import control_node_from_net_env
from nika.problems.support.oracle import (
    srl_cli,
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    ping_fails,
    router_cli,
    srl,
    shell,
    vtysh,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import (
    RouterFaultOps,
    native_fault_ops,
    native_router_profile,
    router_fault_ops,
)
from nika.problems.support.recovery import FileSnapshot
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import (
    attached_end_hosts,
    choice,
    first,
    isp_protocol_for,
    loaded_inventory,
    replace,
    require_role_member,
    role_subset,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.service.containerlab.srl_api import srl_json
from nika.utils.logger import system_logger
from nika.utils.parallel import bounded_parallel_map


def _compile_isp_bgp_inventory(
    scenario: str, *, igp: str, bgp_mode: str, rpki: bool = False, rtbh: bool = False
):
    """Compile the ISP plan for ``scenario`` and return it with its BGP inventory."""
    from nika.net_env.isp.bgp import compile_bgp_plan
    from nika.net_env.isp.identity import isp_topo_from_scenario
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan

    isp_plan = compile_isp_plan(
        IspConfig(topology=isp_topo_from_scenario(scenario), igp=igp)  # type: ignore[arg-type]
    )
    bgp = compile_bgp_plan(isp_plan, bgp_mode, rpki=rpki, rtbh=rtbh)
    assert bgp is not None
    return isp_plan, bgp.inventory


def _ensure_bgp_inventory(problem_cls, ctx) -> tuple[dict, dict]:
    """Return ``(inventory, bgp_inventory)`` with BGP originators for an ISP scenario.

    Uses the loaded scenario inventory when it has originated prefixes, else
    compiles the ISP plan from ``ctx.isp_options`` (or the failure's protocol needs).
    """
    inventory = loaded_inventory(ctx.net_env)
    bgp_inv = inventory.get("bgp")
    if not isinstance(bgp_inv, dict) or not bgp_inv.get("originated"):
        isp_opts = ctx.isp_options or isp_protocol_for(problem_cls, {"bgp"})
        rtbh = ctx.scenario.endswith("_ebgp_rtbh") or bool(isp_opts.get("rtbh"))
        isp_plan, bgp_inv = _compile_isp_bgp_inventory(
            ctx.scenario,
            igp=str(isp_opts.get("igp") or "ospf"),
            bgp_mode=str(isp_opts.get("bgp_mode") or "ebgp"),
            rpki=bool(isp_opts.get("rpki")),
            rtbh=rtbh,
        )
        inventory = dict(isp_plan.inventory)
        inventory["bgp"] = bgp_inv
    return inventory, bgp_inv


def _srl_flat_config(runtime, host: str, path: str) -> tuple[str, ...]:
    output = runtime.exec(host, "sr_cli " + shlex.quote(f"info flat {path}"))
    return tuple(
        sorted(
            line.strip()
            for line in output.splitlines()
            if line.strip().startswith("set ")
        )
    )


def _bgp_peers(net_env, host: str) -> list[str]:
    """Routers holding a BGP session with ``host``: the BGP inventory when the
    scenario has one, else the directly linked routers."""
    bgp = loaded_inventory(net_env).get("bgp")
    sessions = bgp.get("sessions") if isinstance(bgp, dict) else None
    if sessions:
        return sorted(
            {
                str(s["remote_device"])
                for s in sessions
                if s.get("local_device") == host and s.get("remote_device")
            }
        )
    return [peer for _intf, peer, _peer_intf in linked_routers(net_env, host)]


# Read password presence without disclosing RouterOS's sensitive key value.
_ORACLE_ROS_PASSWORDS = (
    ":foreach c in=[/routing bgp connection find] do={ "
    ':put ("peer=" . [/routing bgp connection get $c remote.address] . '
    '" md5=" . ([:len [/routing bgp connection get $c tcp-md5-key]] > 0)) }'
)


def _oracle_bgp_peers(net_env, host: str) -> list[str]:
    """Include logical BGP peers when sessions run over overlay interfaces."""
    declared = getattr(net_env, "bgp_import_peers", lambda: ())()
    local = [peer for peer in declared if peer.node == host]
    remote = {
        peer.node
        for peer in declared
        if peer.node != host
        and any(
            peer.local_asn == target.remote_asn and peer.remote_asn == target.local_asn
            for target in local
        )
    }
    return sorted(remote) if remote else _bgp_peers(net_env, host)


def _oracle_password_peers(output: str) -> list[str]:
    """Neighbors with authentication in observed FRR, XR, or RouterOS output."""
    peers = re.findall(r"^\s*neighbor (\S+) password \S+", output, re.M)
    peers += [
        peer
        for peer, body in re.findall(
            r"^ neighbor (\S+)\n(.*?)^ !$", output, re.M | re.S
        )
        if re.search(r"^  password \S+", body, re.M)
    ]
    peers += [
        peer.split("@", 1)[0].split("/", 1)[0]
        for peer in re.findall(r"^peer=(\S+) md5=true\s*$", output, re.M)
    ]
    peers += re.findall(
        r"^set / network-instance default protocols bgp neighbor (\S+) authentication password \S+",
        output,
        re.M,
    )
    return sorted(set(peers))


def _oracle_bgp_config(output: str) -> bool:
    """Require a recognizable neighbor before checking absent settings."""
    return bool(
        re.search(r"^\s*neighbor \S+ (?:remote-as|interface)", output, re.M)
        or re.search(r"^ neighbor \S+\n\s+remote-as ", output, re.M)
        or re.search(r"^peer=\S+ md5=(?:true|false)\s*$", output, re.M)
        or re.search(
            r"^set / network-instance default protocols bgp neighbor \S+ peer-group ",
            output,
            re.M,
        )
    )


# A ``show bgp summary`` row whose session is not Established.
_BGP_ROW_DOWN = (
    r"^\S+\s+4\s+\d+(?:\s+\d+){5}\s+\S+\s+"
    r"(?:Active|Idle|Connect|OpenSent|OpenConfirm)\b"
)

# A ``show bgp summary`` row of an Established session (prefix count as state).
# Established rows show a prefix count, or ``(Policy)`` when eBGP has no
# policy and exchanges no routes (FRR's default ``ebgp-requires-policy``).
_BGP_ROW_UP = r"^\S+\s+4\s+\d+(?:\s+\d+){5}\s+\S+\s+(?:\d+|\(Policy\))\s"


def _peer_expects_other_asn(output: str) -> bool:
    """A peer's down session toward the host expects an AS other than the
    host's local AS (``host_local_as``/``host_addrs`` lines name the host),
    while another of the peer's sessions is Established, so the peer's own
    AS is accepted elsewhere."""
    if not re.search(_BGP_ROW_UP, output, re.M):
        return False
    local = re.search(r"^host_local_as=(\d+)$", output, re.M)
    addrs = re.search(r"^host_addrs=(\S*)$", output, re.M)
    if not local or not addrs:
        return False
    host_addrs = set(addrs.group(1).split(","))
    for block in re.split(r"(?m)^(?=BGP neighbor )", output):
        m = re.match(r"BGP neighbor (?:is |on \S+: )([^,\s]+), remote AS (\d+)", block)
        if (
            m
            and m.group(1) in host_addrs
            and m.group(2) != local.group(1)
            and "BGP state = Established" not in block
        ):
            return True
    return False


def _speaker_refuses_host(output: str) -> bool:
    """A MetalLB speaker refused the host with "Bad Peer AS": its session to
    one of the host's addresses expects an AS other than the host's local AS
    (``host_local_as`` line), and no speaker session to the host is up."""
    local = re.search(r"^host_local_as=(\d+)$", output, re.M)
    if not local:
        return False
    refused = False
    for block in re.split(r"(?m)^(?=BGP neighbor )", output):
        m = re.match(r"BGP neighbor is ([^,\s]+), remote AS (\d+)", block)
        if not m:
            continue
        if "BGP state = Established" in block:
            return False
        if m.group(2) != local.group(1) and "Bad Peer AS" in block:
            refused = True
    return refused


def _srl_bgp_rows(output: str) -> list[tuple[str, str, str]]:
    """``(peer, peer_as, state)`` rows of an SR Linux BGP neighbor table."""
    return re.findall(
        r"^\|\s*\S+\s*\|\s*(\S+)\s*\|[^|]*\|[^|]*\|\s*(\d+)\s*\|\s*(\w+)\s*\|",
        output,
        re.M,
    )


# ==================================================================
""" Problem: BGP ASN misconfiguration. """
# ==================================================================


# bgp_asn_misconfig moves the local ASN by this fixed offset.
ASN_MISCONFIG_OFFSET = 600


class BGPAsnMisconfigParams(BaseModel):
    """Parameters for injecting a BGP ASN misconfiguration fault."""

    host_name: str = Field(description="Target router host name.")


class BGPAsnMisconfig(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_asn_misconfig"
    description = "BGP local ASN is misconfigured relative to peer expectation."
    effect_protocol = "bgp"
    TAGS: str = ["bgp"]
    INCOMPATIBLE_SCENARIOS = {
        "routeros_simple_bgp": "The injector reads and edits the FRR BGP "
        "configuration, which RouterOS routers do not have."
    }
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset({"bgp_asn", "set_bgp_asn"})

    Params = BGPAsnMisconfigParams
    BENCHMARK_COORDINATES = frozenset({"bgp_acl_block"})

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        # Transit routers without BGP (enterprise ISP cores) have no session.
        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "bgp"
        )

    @classmethod
    def coordinate_benchmark_inject(cls, ctx, params_by_problem):
        # Misconfigure a router other than the ACL-blocked one.
        acl = dict(params_by_problem["bgp_acl_block"])
        asn = dict(params_by_problem["bgp_asn_misconfig"])
        routers = list(ctx.net_env.routers or [])
        if acl.get("host_name") == asn.get("host_name") and len(routers) >= 2:
            asn["host_name"] = (
                routers[1] if routers[0] == acl.get("host_name") else routers[0]
            )
        params_by_problem["bgp_asn_misconfig"] = asn
        return params_by_problem

    symptom_desc = "Some hosts are experiencing connectivity issues."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPAsnMisconfigParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(self, params: BGPAsnMisconfigParams) -> OraclePlaybook:
        host = params.host_name
        peers = _bgp_peers(self.net_env, host)
        if not peers:
            raise RuntimeCapabilityError(f"No BGP peer of {host} in the topology.")
        peer = peers[0]
        if srl_cli(self.net_env, host):
            return self._srl_playbook(host, peer)
        control = control_node_from_net_env(self.net_env)
        if control and getattr(self.net_env, "kubernetes_nodes", None):
            return self._metallb_playbook(host, control)

        def peer_view(outputs) -> dict:
            text = outputs.get("host_bgp", "")
            local = re.search(r"local AS number (\d+)", text)
            addrs = re.findall(r"^addr ([^/\s]+)", text, re.M)
            wanted = "|".join(re.escape(a) for a in addrs) or "none"
            # Every session row of the peer, then the neighbor blocks (header
            # and state) of its sessions toward the host's addresses.
            command = (
                f"echo host_local_as={local.group(1) if local else ''}; "
                f"echo host_addrs={','.join(addrs)}; "
                + vtysh("show bgp summary")
                + " | grep -E '^[^ ]+ +4 '; "
                + vtysh("show bgp neighbors")
                + " | awk '/^BGP neighbor /{p = ($0 ~ /^BGP neighbor (is|on [^ ]+:) "
                f"({wanted}),/)}} p && /^BGP neighbor |BGP state|Last reset/'"
            )
            return {"host_name": peer, "command": command, "timeout": 20}

        return OraclePlaybook(
            finding=(
                f"{host} runs BGP under the wrong local AS, which its peers do not "
                "expect, so its BGP sessions cannot establish."
            ),
            steps=[
                shell(
                    host,
                    vtysh("show bgp summary")
                    + "; ip -o addr show | awk '{print \"addr \" $4}'",
                    f"Read {host}'s local AS and the state of its BGP sessions.",
                    all_of(
                        contains(r"local AS number \d+", "bgpd answers"),
                        contains(_BGP_ROW_DOWN, "its BGP sessions are down"),
                        lacks(_BGP_ROW_UP, "none of its sessions is Established"),
                    ),
                    evidence=True,
                    save_as="host_bgp",
                ),
                shell(
                    host,
                    vtysh("show bgp neighbors")
                    + " | grep -E '^BGP neighbor |BGP state|Last reset|Bad Peer'",
                    f"Check why {host}'s sessions reset.",
                ),
                OracleStep(
                    f"Compare the AS that peer {peer} expects for {host} with "
                    f"{host}'s local AS.",
                    "exec_shell",
                    peer_view,
                    Check(
                        f"{peer}'s down session to {host} expects a different AS "
                        f"than {host} runs, while {peer}'s other sessions are up",
                        _peer_expects_other_asn,
                    ),
                    evidence=True,
                ),
            ],
        )

    def _metallb_playbook(self, host: str, control: str) -> OraclePlaybook:
        """Kubernetes fabric: the MetalLB speakers pin the router's AS, while
        its fabric peers accept any external AS and come back up."""

        def speaker_view(outputs) -> dict:
            text = outputs.get("host_bgp", "")
            local = re.search(r"local AS number (\d+)", text)
            addrs = " ".join(re.findall(r"^addr ([^/\s]+)", text, re.M))
            peers_jsonpath = "{.items[*].spec.peerAddress}"
            command = (
                f"echo host_local_as={local.group(1) if local else ''}; "
                "for a in $(kubectl get bgppeers.metallb.io -A "
                f"-o jsonpath='{peers_jsonpath}'); do "
                f'case " {addrs} " in *" $a "*) '
                "for p in $(kubectl -n metallb-system get pods "
                "-l component=speaker -o name); do "
                "kubectl -n metallb-system exec ${p#pod/} -c frr -- "
                'vtysh -c "show bgp neighbors $a" 2>/dev/null '
                "| grep -E '^BGP neighbor|BGP state|Last reset'; done;; esac; done"
            )
            return {"host_name": control, "command": command, "timeout": 90}

        return OraclePlaybook(
            finding=(
                f"{host} runs BGP under the wrong local AS; the MetalLB speakers "
                "that peer with it expect its configured AS and refuse the session, "
                "so the service routes they announce no longer reach it."
            ),
            steps=[
                shell(
                    host,
                    vtysh("show bgp summary")
                    + "; "
                    + vtysh("show running-config")
                    + " | grep -E 'listen range'"
                    + "; ip -o addr show | awk '{print \"addr \" $4}'",
                    f"Read {host}'s local AS, its BGP sessions, and the dynamic "
                    "peers it listens for.",
                    all_of(
                        contains(r"local AS number \d+", "bgpd answers"),
                        contains(r"bgp listen range", "it listens for dynamic peers"),
                        lacks(
                            r"^\*" + _BGP_ROW_UP[1:], "no dynamic peer session is up"
                        ),
                    ),
                    evidence=True,
                    save_as="host_bgp",
                ),
                OracleStep(
                    f"Ask the MetalLB speakers why their sessions to {host} are down.",
                    "exec_shell",
                    speaker_view,
                    Check(
                        f"a MetalLB speaker expects a different AS for {host} than "
                        f"{host} runs and refused it with Bad Peer AS",
                        _speaker_refuses_host,
                    ),
                    evidence=True,
                    # Speakers must retry TCP before rejecting the new AS.
                    attempts=6,
                    interval_sec=30,
                ),
            ],
        )

    def _srl_playbook(self, host: str, peer: str) -> OraclePlaybook:
        """SR Linux: compare the host's AS with the peer-AS its peer expects."""
        neighbors = "show network-instance default protocols bgp neighbor"
        seen: dict = {}

        def peer_view(outputs) -> dict:
            # The peer check compares against the AS and addresses read on
            # the host.
            text = outputs.get("host_as", "")
            m = re.search(r"autonomous-system (\d+)", text)
            seen["asn"] = m.group(1) if m else ""
            seen["addrs"] = set(
                re.findall(r"ipv4 address ([\d.]+)/", outputs.get("host_addrs", ""))
            )
            return {"device_name": peer, "command": neighbors}

        def expects_other(output: str) -> bool:
            rows = _srl_bgp_rows(output)
            toward_host = [r for r in rows if r[0] in seen.get("addrs", ())]
            return (
                bool(seen.get("asn"))
                and any(
                    state != "established" and peer_as != seen["asn"]
                    for _addr, peer_as, state in toward_host
                )
                and any(
                    state == "established"
                    for addr, _as, state in rows
                    if addr not in seen.get("addrs", ())
                )
            )

        def host_all_down(output: str) -> bool:
            rows = _srl_bgp_rows(output)
            return bool(rows) and all(state != "established" for *_, state in rows)

        return OraclePlaybook(
            finding=(
                f"{host} runs BGP under the wrong local AS, which its peers do not "
                "expect, so its BGP sessions cannot establish."
            ),
            steps=[
                OracleStep(
                    f"List {host}'s BGP sessions.",
                    "srl_exec_cli",
                    {"device_name": host, "command": neighbors},
                    Check(f"{host} has sessions and none is up", host_all_down),
                    evidence=True,
                ),
                OracleStep(
                    f"Read {host}'s configured BGP AS.",
                    "srl_exec_cli",
                    {
                        "device_name": host,
                        "command": "info flat network-instance default protocols "
                        "bgp autonomous-system",
                    },
                    contains(r"autonomous-system \d+", f"{host}'s local AS"),
                    save_as="host_as",
                ),
                OracleStep(
                    f"Read {host}'s interface addresses.",
                    "srl_exec_cli",
                    {
                        "device_name": host,
                        "command": "info flat interface * subinterface * ipv4 address *",
                    },
                    contains(r"ipv4 address [\d.]+/", f"{host}'s IPv4 addresses"),
                    save_as="host_addrs",
                ),
                OracleStep(
                    f"Compare the AS that peer {peer} expects for {host}.",
                    "srl_exec_cli",
                    peer_view,
                    Check(
                        f"{peer}'s down session to {host} expects an AS other than "
                        f"{host}'s, while {peer}'s other sessions are up",
                        expects_other,
                    ),
                    evidence=True,
                ),
            ],
        )

    def inject_fault(self, params: BGPAsnMisconfigParams):
        match self.lab_backend:
            case "containerlab":
                self._inject_asn_misconfig_containerlab(params)
            case "kathara":
                self._inject_asn_misconfig_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_asn_misconfig_containerlab(self, params: BGPAsnMisconfigParams) -> None:
        if ops := router_fault_ops(self, params.host_name):
            self._inject_asn_misconfig_native(params, ops)
            return
        as_number = self.runtime.srl_get_bgp_as(params.host_name)
        self._orig_asn = as_number
        wrong_asn = as_number + ASN_MISCONFIG_OFFSET
        self.runtime.srl_set_bgp_as(params.host_name, wrong_asn)
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} "
            f"from ASN {as_number} to {wrong_asn} (SRL)."
        )

    def _inject_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> None:
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            self._inject_asn_misconfig_native(params, ops)
            return
        as_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
        wrong_asn = as_number + ASN_MISCONFIG_OFFSET
        self._asn_running_config = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config'"
        )
        self._asn_config_files = {}
        self._orig_asn = as_number
        # k8s_lab uses split FRR configs (/etc/frr/bgpd.conf); campus etc. use frr.conf.
        # Patch the on-disk config so it matches what the running bgpd will use.
        # Avoid shell "$vars" inside runtime.exec wrapping (quote escaping breaks loops).
        patched: list[str] = []
        for conf in ("/etc/frr/frr.conf", "/etc/frr/bgpd.conf"):
            exists = self.runtime.exec(
                params.host_name,
                f"test -f {conf} && echo yes || echo no",
                timeout=10,
            ).strip()
            if exists != "yes":
                continue
            self._asn_config_files[conf] = FileSnapshot.capture(
                self.runtime, params.host_name, conf
            )
            self.runtime.exec(
                params.host_name,
                f"sed -i -E 's/^router bgp [0-9]+/router bgp {wrong_asn}/' {conf}",
                timeout=10,
            )
            patched.append(conf)
        verify_files = self.runtime.exec(
            params.host_name,
            "grep -E '^router bgp' /etc/frr/frr.conf /etc/frr/bgpd.conf 2>/dev/null || true",
            timeout=10,
        )
        if not patched or f"router bgp {wrong_asn}" not in verify_files:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: could not patch BGP ASN files on "
                f"{params.host_name!r}: patched={patched!r} grep={verify_files!r}"
            )
        # FRR cannot renumber a live BGP instance in place, so delete it and
        # reload the patched config through vtysh. The daemons keep running:
        # killing them leaves watchfrr holding its pid lock, so a restart fails
        # and bgpd stays down instead of running with the wrong ASN (#265).
        # VRF instances depend on the default one and must be removed first.
        instances = [
            line.strip()
            for line in self.runtime.exec(
                params.host_name,
                "vtysh -c 'show running-config' 2>/dev/null | grep -E '^router bgp '",
                timeout=20,
            ).splitlines()
            if line.startswith("router bgp ")
        ]
        instances.sort(key=lambda line: " vrf " not in line)
        if instances:
            remove = " ".join(f"-c 'no {line}'" for line in instances)
            self.runtime.exec(
                params.host_name,
                f"vtysh -c 'configure terminal' {remove}",
                timeout=30,
            )
        for conf in patched:
            self.runtime.exec(params.host_name, f"vtysh -f {conf}", timeout=60)
        wait_until(lambda: self._running_asn(params.host_name) == wrong_asn, 30)
        running = self._running_asn(params.host_name)
        if running != wrong_asn:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: FRR on {params.host_name!r} "
                f"did not apply ASN change ({as_number} -> {wrong_asn}); "
                f"running={running}."
            )
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} from ASN {as_number} to {wrong_asn}."
        )

    def _inject_asn_misconfig_native(
        self, params: BGPAsnMisconfigParams, ops: RouterFaultOps
    ) -> None:
        as_number = ops.bgp_asn()
        self._orig_asn = as_number
        wrong_asn = as_number + ASN_MISCONFIG_OFFSET
        ops.set_bgp_asn(wrong_asn)
        wait_until(lambda: ops.bgp_asn() == wrong_asn, 8)
        running = ops.bgp_asn()
        if running != wrong_asn:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: {ops.profile} on {params.host_name!r} "
                f"did not apply ASN change ({as_number} -> {wrong_asn}); "
                f"running={running}."
            )
        self._orig_asn = as_number
        self._wrong_asn = wrong_asn
        self.logger.info(
            f"Injected BGP ASN misconfiguration on {params.host_name} "
            f"from ASN {as_number} to {wrong_asn} ({ops.profile})."
        )

    def recover_fault(self, params: BGPAsnMisconfigParams) -> dict:
        original = getattr(self, "_orig_asn", None)
        if original is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        host = params.host_name
        if self.lab_backend == "containerlab":
            self.runtime.srl_set_bgp_as(host, original)
            restored = self.runtime.srl_get_bgp_as(host) == original
        elif (ops := router_fault_ops(self, host)) is not None:
            ops.set_bgp_asn(original)
            restored = wait_until(lambda: ops.bgp_asn() == original, 30)
        else:
            pattern = r"^router bgp .+\n(?:[ \t].*\n|!\n)*"
            blocks = re.findall(pattern, self._asn_running_config, re.M)
            running = self.runtime.exec(host, "vtysh -c 'show running-config'")
            if (
                re.findall(pattern, running, re.M) == blocks
                and self._running_asn(host) == original
                and all(
                    snapshot.matches(self.runtime, host)
                    for snapshot in self._asn_config_files.values()
                )
            ):
                return build_verify_result(
                    self.root_cause_name, True, {"host": host, "original_asn": original}
                )
            instances = sorted(
                re.findall(r"^router bgp .+$", running, re.M),
                key=lambda line: " vrf " not in line,
            )
            if instances:
                self.runtime.exec(
                    host, frr_config_command(*(f"no {line}" for line in instances))
                )
            # Reapply the original BGP instances, including every VRF and address family.
            for block in blocks:
                lines = [
                    line.strip() for line in block.splitlines() if line.strip() != "!"
                ]
                self.runtime.exec(host, frr_config_command(*lines))
            for snapshot in self._asn_config_files.values():
                snapshot.restore(self.runtime, host)
            restored = wait_until(lambda: self._running_asn(host) == original, 30)
            restored = restored and all(
                snapshot.matches(self.runtime, host)
                for snapshot in self._asn_config_files.values()
            )
            running = self.runtime.exec(host, "vtysh -c 'show running-config'")
            restored = restored and re.findall(pattern, running, re.M) == blocks
        return build_verify_result(
            self.root_cause_name,
            bool(restored),
            {"host": host, "original_asn": original},
        )

    def verify_fault(self, params: BGPAsnMisconfigParams) -> dict:
        """Verify the ASN in frr.conf or SRL running config was changed."""
        match self.lab_backend:
            case "containerlab":
                return self._verify_asn_misconfig_containerlab(params)
            case "kathara":
                return self._verify_asn_misconfig_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_asn_misconfig_containerlab(self, params: BGPAsnMisconfigParams) -> dict:
        if ops := router_fault_ops(self, params.host_name):
            return self._verify_asn_misconfig_native(params, ops)
        running_asn = self.runtime.srl_get_bgp_as(params.host_name)
        orig_asn = getattr(self, "_orig_asn", None)
        wrong_asn = getattr(self, "_wrong_asn", None)
        verified = (wrong_asn is not None and running_asn == wrong_asn) or (
            orig_asn is not None and running_asn != orig_asn
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "orig_asn": orig_asn,
                "running_asn": running_asn,
                "wrong_asn": wrong_asn,
            },
        )

    def _running_asn(self, host: str) -> int | None:
        """Local AS of the running bgpd, or ``None`` while it does not answer."""
        summary = self.runtime.exec(
            host, "vtysh -c 'show bgp summary json' 2>/dev/null"
        )
        match = re.search(r'"as"\s*:\s*(\d+)', summary)
        return int(match.group(1)) if match else None

    def _verify_asn_misconfig_kathara(self, params: BGPAsnMisconfigParams) -> dict:
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            return self._verify_asn_misconfig_native(params, ops)
        orig_asn = getattr(self, "_orig_asn", None)
        expected_wrong = getattr(self, "_wrong_asn", None)
        # Only the running bgpd counts: the on-disk file is what inject wrote.
        running_asn = self._running_asn(params.host_name)
        verified = expected_wrong is not None and running_asn == expected_wrong
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "orig_asn": orig_asn,
                "running_asn": running_asn,
                "wrong_asn": expected_wrong,
            },
        )

    def _verify_asn_misconfig_native(
        self, params: BGPAsnMisconfigParams, ops: RouterFaultOps
    ) -> dict:
        orig_asn = getattr(self, "_orig_asn", None)
        wrong_asn = getattr(self, "_wrong_asn", None)
        try:
            running_asn = ops.bgp_asn()
        except RuntimeCapabilityError:
            raise
        except Exception:
            running_asn = None
        verified = (wrong_asn is not None and running_asn == wrong_asn) or (
            orig_asn is not None and running_asn is not None and running_asn != orig_asn
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "orig_asn": orig_asn,
                "running_asn": running_asn,
                "wrong_asn": wrong_asn,
            },
        )


# ==================================================================
""" Problem: BGP missing route advertisement. """
# ==================================================================


class BGPMissingAdvertiseParams(BaseModel):
    """Parameters for injecting a BGP missing route advertisement fault."""

    host_name: str = Field(description="Target router host name.")
    prefix: str | None = Field(
        default=None,
        description="Optional BGP prefix to withdraw (ISP originators).",
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional probe source host for symptom checks.",
    )
    probe_dst_ip: str | None = Field(
        default=None,
        description="Optional probe destination IP for symptom checks.",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer/stub host near the victim originator.",
    )


class BGPMissingAdvertise(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    # Remote speakers lose the route. Reachability can survive when the IGP
    # also carries the prefix (see INCOMPATIBLE_SCENARIOS).
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_missing_route_advertisement"
    description = "An expected BGP route advertisement is missing."
    effect_property = "reachability"
    TAGS: str = ["bgp"]
    INCOMPATIBLE_SCENARIOS = {
        "min3clos": "The IGP also carries the host subnets, so peers keep a route "
        "after BGP stops advertising it.",
        "routeros_simple_bgp": "The injector reads and edits the FRR BGP "
        "configuration, which RouterOS routers do not have.",
    }
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset(
        {"bgp_networks", "withdraw_bgp_network", "save_config", "restore_config"}
    )

    Params = BGPMissingAdvertiseParams

    BENCHMARK_TARGET_ROLE = "bgp_originators"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            from nika.net_env.isp.inject_targets import isp_inject_params

            inventory, bgp_inv = _ensure_bgp_inventory(cls, ctx)
            return dict(isp_inject_params(ctx.problem, inventory, bgp_inv))
        advertise_pool = role_subset(ctx.router_pool, ctx.roles, "bgp_originators")
        return {
            "host_name": choice(
                ctx.rng, advertise_pool, first(advertise_pool) or ctx.router0
            )
        }

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        from nika.net_env.isp.identity import is_isp_scenario

        if not is_isp_scenario(ctx.scenario):
            return super().benchmark_inject_options(ctx, base)
        from nika.net_env.isp.inject_targets import enrich_isp_symptom_params

        inventory, bgp = _ensure_bgp_inventory(cls, ctx)
        rows = []
        for item in sorted(
            bgp.get("originated") or [],
            key=lambda row: (
                str(row.get("device") or ""),
                str(row.get("prefix") or ""),
            ),
        ):
            host = str(item.get("device") or "")
            prefix = str(item.get("prefix") or "")
            if not host:
                continue
            row = replace(base, host_name=host)
            if prefix:
                row["prefix"] = prefix
            for key in ("symptom_host", "probe_dst_ip", "peer_host"):
                row.pop(key, None)
            enrich_isp_symptom_params(row, ctx.problem, inventory, bgp)
            rows.append(row)
        return rows

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.net_env.isp.identity import is_isp_scenario

        host_name = inject.get("host_name")
        if not host_name:
            return
        if not is_isp_scenario(ctx.scenario):
            require_role_member(
                ctx,
                cls.root_cause_name,
                host_name,
                "bgp_originators",
                reason="has no BGP network statement",
            )
            return
        bgp_inv = loaded_inventory(ctx.net_env).get("bgp")
        if isinstance(bgp_inv, dict) and bgp_inv.get("originated"):
            originators = sorted(
                {
                    str(item["device"])
                    for item in bgp_inv["originated"]
                    if item.get("device")
                }
            )
            if originators and host_name not in originators:
                raise ValueError(
                    f"{cls.root_cause_name} host_name={host_name!r} is "
                    f"not a BGP originator on {ctx.scenario}; use one of: {originators}"
                )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._inject_mode: str | None = None
        self._withdrawn_prefix: str | None = None

    def root_cause_resources(self, params: BGPMissingAdvertiseParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(self, params: BGPMissingAdvertiseParams) -> OraclePlaybook:
        host = params.host_name
        sessions_up = shell(
            host,
            vtysh("show bgp summary"),
            f"Check that {host}'s BGP sessions are up.",
            contains(_BGP_ROW_UP, "at least one BGP session is Established"),
            evidence=True,
        )
        if self._is_enterprise_branch():
            return self._enterprise_playbook(host, sessions_up)
        bgp_block = (
            vtysh("show running-config")
            + " | sed -n '/^router bgp/,/^exit$/p' | grep -E '^router bgp|network|redistribute'"
        )
        if not params.prefix:
            return OraclePlaybook(
                finding=f"{host} no longer originates its networks into BGP.",
                steps=[
                    shell(
                        host,
                        vtysh("show bgp ipv4 unicast"),
                        f"List the BGP routes on {host}.",
                        all_of(
                            contains(r"BGP table version", "the BGP table is listed"),
                            lacks(
                                r"^\*>\s*\S+\s+0\.0\.0\.0\s",
                                f"{host} originates no route into BGP",
                            ),
                        ),
                        evidence=True,
                    ),
                    shell(
                        host,
                        "ip -br addr",
                        f"List {host}'s connected networks that BGP should carry.",
                    ),
                    sessions_up,
                    shell(
                        host,
                        bgp_block,
                        f"Inspect {host}'s BGP origination config.",
                        all_of(
                            contains(r"^router bgp \d+", "the BGP config is listed"),
                            lacks(r"^\s+network ", "no BGP network statement remains"),
                        ),
                        evidence=True,
                    ),
                ],
            )
        prefix = str(ipaddress.ip_network(params.prefix, strict=False))
        steps = []
        if params.symptom_host:
            steps.append(
                shell(
                    params.symptom_host,
                    vtysh(f"show bgp ipv4 unicast {prefix}"),
                    f"Check whether {params.symptom_host} learns {prefix}.",
                    contains(
                        r"Network not in table", f"{params.symptom_host} lacks {prefix}"
                    ),
                )
            )
        steps += [
            shell(
                host,
                vtysh(f"show ip route {prefix}"),
                f"Confirm {prefix} exists on its originator {host}.",
                contains(
                    r'Known via "(connected|static|kernel|local)"',
                    f"{host} has a local route for {prefix}",
                ),
                evidence=True,
            ),
            shell(
                host,
                vtysh(f"show bgp ipv4 unicast {prefix}"),
                f"Check whether {host} carries {prefix} in BGP.",
                contains(r"Network not in table", f"{prefix} is not in {host}'s BGP"),
                evidence=True,
            ),
            sessions_up,
            shell(
                host,
                bgp_block,
                f"Inspect {host}'s BGP origination config.",
                all_of(
                    contains(r"^router bgp \d+", "the BGP config is listed"),
                    lacks(
                        rf"^\s+network {re.escape(prefix)}\b",
                        f"no BGP network statement for {prefix}",
                    ),
                ),
                evidence=True,
            ),
        ]
        return OraclePlaybook(
            finding=(
                f"{host} no longer originates {prefix} into BGP, so its peers lose "
                "the route although the network and the sessions are up."
            ),
            steps=steps,
        )

    def _enterprise_playbook(self, host: str, sessions_up) -> OraclePlaybook:
        """Branch edges advertise their VRF LANs by redistributing connected."""
        return OraclePlaybook(
            finding=(
                f"{host}'s BGP VRF instances no longer redistribute its connected "
                "LANs, so the rest of the overlay loses the routes to its site."
            ),
            steps=[
                shell(
                    host,
                    vtysh("show ip route vrf all connected") + " | grep -E '^IPv4|^C'",
                    f"List the site LANs attached to {host}'s VRFs.",
                    contains(
                        r"^IPv4 unicast VRF vrf_\S+:\nC>\*",
                        f"{host} has connected LANs in its VRFs",
                    ),
                    evidence=True,
                ),
                shell(
                    host,
                    vtysh("show bgp vrf all ipv4 unicast")
                    + " | grep -E '^Instance|^BGP table|32768|Displayed'",
                    f"Check which routes {host} originates into BGP.",
                    all_of(
                        contains(
                            r"^BGP table version", "the VRF BGP tables are listed"
                        ),
                        lacks(r"\b32768\b", f"{host} originates no route into BGP"),
                    ),
                    evidence=True,
                ),
                sessions_up,
                shell(
                    host,
                    vtysh("show running-config") + " | sed -n '/^router bgp/,/^exit$/p'"
                    " | grep -E '^router bgp|redistribute|network'",
                    f"Inspect how {host}'s BGP instances originate the site LANs.",
                    all_of(
                        contains(r"^router bgp \d+ vrf ", "VRF BGP instances exist"),
                        lacks(
                            r"^\s+(redistribute connected|network )",
                            "none redistributes connected or has a network statement",
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )

    def _is_enterprise_branch(self) -> bool:
        name = (self.scenario_name or "").lower()
        return name == "enterprise_branch" or name.startswith("enterprise_branch")

    def inject_fault(self, params: BGPMissingAdvertiseParams):
        host = params.host_name
        if self.lab_backend == "kathara":
            ops = router_fault_ops(self, host)
            if ops is not None:
                self._advertise_native_config = ops.save_config()
                self._advertise_networks = ops.bgp_networks()
            else:
                self._advertise_running = self.runtime.exec(
                    host, "vtysh -c 'show running-config'"
                )
        elif self.lab_backend == "containerlab":
            self._advertise_srl = {
                path: _srl_flat_config(self.runtime, host, path)
                for path in ("routing-policy", "network-instance default protocols bgp")
            }
            if not self._advertise_srl["network-instance default protocols bgp"]:
                raise RuntimeCapabilityError(
                    f"Cannot snapshot BGP configuration on {host!r} before withdrawal."
                )
        match self.lab_backend:
            case "containerlab":
                self._inject_missing_adv_containerlab(params)
            case "kathara":
                self._inject_missing_adv_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _resolve_withdraw_prefix(self, params: BGPMissingAdvertiseParams) -> str:
        if params.prefix:
            return str(ipaddress.ip_network(params.prefix, strict=False))
        return str(
            ipaddress.ip_network(
                resolve_victim_host_ip(self.runtime, params.host_name),
                strict=False,
            )
        )

    def _inject_missing_adv_containerlab(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        if ops := router_fault_ops(self, params.host_name):
            self._inject_missing_adv_native(params, ops)
            return
        prefix = self._resolve_withdraw_prefix(params)
        self._withdrawn_prefix = prefix
        self._inject_mode = "srl_prefix"
        self.runtime.srl_withdraw_bgp_prefix(params.host_name, prefix)
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(SRL export-policy block for {prefix})."
        )

    def _inject_missing_adv_kathara(self, params: BGPMissingAdvertiseParams) -> None:
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            self._inject_missing_adv_native(params, ops)
            return
        if self._is_enterprise_branch():
            self._inject_missing_adv_enterprise_redistribute(params)
            return
        if params.prefix:
            self._inject_missing_adv_prefix(params, params.prefix)
            return
        self._inject_missing_adv_bgp_networks(params)

    def _inject_missing_adv_native(
        self, params: BGPMissingAdvertiseParams, ops: RouterFaultOps
    ) -> None:
        if params.prefix:
            prefix = str(ipaddress.ip_network(params.prefix, strict=False))
            ops.withdraw_bgp_network(prefix)
            self._withdrawn_prefix = prefix
            self._inject_mode = "native_prefix"
            self.logger.info(
                f"Injected BGP missing route on {params.host_name} "
                f"({ops.profile} withdrew network {prefix})."
            )
            return
        prefixes = ops.bgp_networks()
        if not prefixes:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: no BGP network statements on "
                f"{params.host_name!r} to withdraw ({ops.profile})."
            )
        for prefix in prefixes:
            ops.withdraw_bgp_network(prefix)
        self._inject_mode = "native_bgp_network"
        if len(prefixes) == 1:
            self._withdrawn_prefix = prefixes[0]
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"({ops.profile} withdrew BGP networks {prefixes})."
        )

    def _inject_missing_adv_prefix(
        self, params: BGPMissingAdvertiseParams, prefix: str
    ) -> None:
        prefix = str(ipaddress.ip_network(prefix, strict=False))
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        cmd = frr_config_command(
            f"router bgp {asn}",
            f"no network {prefix}",
            "address-family ipv4 unicast",
            f"no network {prefix}",
        )
        self.runtime.exec(params.host_name, cmd)
        frr_save_config(self.runtime, params.host_name)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true",
        )
        self._withdrawn_prefix = prefix
        self._inject_mode = "prefix"
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(withdrew network {prefix})."
        )

    def _inject_missing_adv_bgp_networks(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        """Withdraw every BGP ``network`` statement via vtysh (leave OSPF alone)."""
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        cfg = self.runtime.exec(
            params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
        )
        prefixes: list[str] = []
        in_bgp = False
        for line in cfg.splitlines():
            s = line.lstrip()
            if s.startswith("router bgp"):
                in_bgp = True
                continue
            if s.startswith("router ") and not s.startswith("router bgp"):
                in_bgp = False
                continue
            if not in_bgp or not s.startswith("network "):
                continue
            rest = s[len("network ") :].strip()
            # OSPF uses ``network … area …``; BGP uses ``network <prefix>``.
            if " area " in rest:
                continue
            token = rest.split()[0] if rest else ""
            if token:
                prefixes.append(token)
        if not prefixes:
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: no BGP network statements on "
                f"{params.host_name!r} to withdraw."
            )
        lines = [f"router bgp {asn}"]
        for prefix in prefixes:
            lines += [f"no network {prefix}"]
        lines.append("address-family ipv4 unicast")
        for prefix in prefixes:
            lines.append(f"no network {prefix}")
        self.runtime.exec(params.host_name, frr_config_command(*lines))
        frr_save_config(self.runtime, params.host_name)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true",
        )
        self._inject_mode = "bgp_network"
        if len(prefixes) == 1:
            self._withdrawn_prefix = prefixes[0]
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            f"(withdrew BGP networks {prefixes})."
        )

    def _redistributing_vrfs(self, host: str) -> list[str]:
        """BGP VRF instances on *host* that redistribute connected routes."""
        config = self.runtime.exec(host, "vtysh -c 'show running-config'")
        vrfs: list[str] = []
        vrf = None
        for line in config.splitlines():
            if not line.startswith(" "):
                header = re.match(r"router bgp \d+ vrf (\S+)", line)
                vrf = header.group(1) if header else None
            elif vrf and line.strip() == "redistribute connected" and vrf not in vrfs:
                vrfs.append(vrf)
        return vrfs

    def _inject_missing_adv_enterprise_redistribute(
        self, params: BGPMissingAdvertiseParams
    ) -> None:
        asn = self.runtime.frr_get_bgp_asn_number(params.host_name)
        # Overlay VRFs advertise LAN prefixes via redistribute connected. Edit
        # only the VRF instances the router has: entering a missing one would
        # create an empty instance that was not there before the fault.
        vrfs = self._redistributing_vrfs(params.host_name)
        if not vrfs:
            raise RuntimeCapabilityError(
                f"{params.host_name!r} has no BGP VRF that redistributes "
                "connected routes."
            )
        lines: list[str] = []
        for vrf in vrfs:
            lines += [
                f"router bgp {asn} vrf {vrf}",
                "address-family ipv4 unicast",
                "no redistribute connected",
                "exit",
                "exit",
            ]
        cmd = frr_config_command(*lines)
        self.runtime.exec(params.host_name, f"{cmd} 2>/dev/null || true")
        frr_save_config(self.runtime, params.host_name)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true",
        )
        self._inject_mode = "redistribute"
        self.logger.info(
            f"Injected BGP missing route on {params.host_name} "
            "(disabled redistribute connected under BGP VRFs)."
        )

    def recover_fault(self, params: BGPMissingAdvertiseParams) -> dict:
        host = params.host_name
        if self.lab_backend == "containerlab":
            snapshot = getattr(self, "_advertise_srl", None)
            if snapshot is None:
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )
            from nika.service.containerlab.srl_api import (
                BGP_EXPORT_POLICY,
                BGP_EXPORT_PREFIX_SET,
            )

            if all(
                _srl_flat_config(self.runtime, host, path) == config
                for path, config in snapshot.items()
            ):
                return build_verify_result(
                    self.root_cause_name,
                    True,
                    {"host": host, "configuration_restored": True},
                )
            peers = {
                match[1]
                for line in snapshot["network-instance default protocols bgp"]
                if (
                    match := re.match(
                        r"set /\s*network-instance default protocols bgp neighbor (\S+)",
                        line,
                    )
                )
            }
            commands = []
            for peer in sorted(peers):
                af = f"/network-instance default protocols bgp neighbor {peer} afi-safi ipv4-unicast"
                existed = any(
                    re.match(
                        rf"set /\s*network-instance default protocols bgp neighbor {re.escape(peer)} afi-safi ipv4-unicast(?: |$)",
                        line,
                    )
                    for line in snapshot["network-instance default protocols bgp"]
                )
                # Remove the AF container when injection created it for an inherited policy.
                commands.append(f"delete {af}" + (" export-policy" if existed else ""))
            commands += [
                f"delete /routing-policy policy {BGP_EXPORT_POLICY}",
                f"delete /routing-policy prefix-set {BGP_EXPORT_PREFIX_SET}",
            ]
            for config in snapshot.values():
                commands.extend(line.removeprefix("set ") for line in config)
            script = (
                "sr_cli <<'NIKA_RECOVER'\nenter candidate\n"
                + "\n".join(commands)
                + "\ncommit now\nNIKA_RECOVER"
            )
            self.runtime.exec(host, script)
            restored = all(
                _srl_flat_config(self.runtime, host, path) == text
                for path, text in snapshot.items()
            )
        elif (ops := router_fault_ops(self, host)) is not None:
            if not hasattr(self, "_advertise_native_config"):
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )
            ops.restore_config(self._advertise_native_config)
            restored = set(ops.bgp_networks()) == set(self._advertise_networks)
        else:
            snapshot = getattr(self, "_advertise_running", None)
            if snapshot is None:
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )

            def original_statements(config):
                statements = []
                router = None
                family = None
                for line in config.splitlines():
                    text = line.strip()
                    if line.startswith("router bgp "):
                        router, family = text, None
                    elif line and not line[0].isspace():
                        router, family = None, None
                    elif router and text.startswith("address-family "):
                        family = text
                    elif text == "exit-address-family":
                        family = None
                    elif router and (
                        (
                            self._is_enterprise_branch()
                            and text.startswith("redistribute connected")
                        )
                        or (
                            not self._is_enterprise_branch()
                            and text.startswith("network ")
                            and (
                                not params.prefix
                                or text.split()[1]
                                == str(
                                    ipaddress.ip_network(params.prefix, strict=False)
                                )
                            )
                        )
                    ):
                        statements.append((router, family, text))
                return statements

            statements = original_statements(snapshot)
            for router, family, statement in statements:
                lines = [router, *([family] if family else []), statement]
                self.runtime.exec(host, frr_config_command(*lines))
            frr_save_config(self.runtime, host)
            running = self.runtime.exec(host, "vtysh -c 'show running-config'")
            restored = bool(statements) and set(statements).issubset(
                original_statements(running)
            )
        return build_verify_result(
            self.root_cause_name,
            bool(restored),
            {"host": host, "configuration_restored": bool(restored)},
        )

    def verify_fault(self, params: BGPMissingAdvertiseParams) -> dict:
        """Verify route withdrawal in frr.conf or SRL BGP export-policy."""
        match self.lab_backend:
            case "containerlab":
                return self._verify_missing_adv_containerlab(params)
            case "kathara":
                return self._verify_missing_adv_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_missing_adv_containerlab(
        self, params: BGPMissingAdvertiseParams
    ) -> dict:
        if ops := router_fault_ops(self, params.host_name):
            return self._verify_missing_adv_native(params, ops)
        prefix = getattr(
            self, "_withdrawn_prefix", None
        ) or self._resolve_withdraw_prefix(params)
        verified = self.runtime.srl_bgp_prefix_withdrawn(params.host_name, prefix)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "prefix": prefix,
                "mode": getattr(self, "_inject_mode", None) or "srl_prefix",
            },
        )

    def _count_running_networks(self, host: str, *, section: str) -> int:
        """Count ``network`` lines under ``router bgp`` or ``router ospf``."""
        if section == "bgp":
            awk = (
                "BEGIN{in_sec=0} "
                "/^router bgp/{in_sec=1; next} "
                "/^router /{in_sec=0} "
                "in_sec && /^[[:space:]]*network /{c++} "
                "END{print c+0}"
            )
        else:
            awk = (
                "BEGIN{in_sec=0} "
                "/^router ospf/{in_sec=1; next} "
                "/^router /{in_sec=0} "
                "in_sec && /^[[:space:]]*network /{c++} "
                "END{print c+0}"
            )
        raw = self.runtime.exec(
            host,
            f"vtysh -c 'show running-config' 2>/dev/null | awk '{awk}' || echo 0",
        ).strip()
        try:
            return int(raw.splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    def _verify_missing_adv_kathara(self, params: BGPMissingAdvertiseParams) -> dict:
        ops = router_fault_ops(self, params.host_name)
        if ops is not None:
            return self._verify_missing_adv_native(params, ops)
        mode = self._inject_mode
        if mode is None:
            if self._is_enterprise_branch():
                mode = "redistribute"
            elif params.prefix:
                mode = "prefix"
            else:
                mode = "bgp_network"

        if mode == "redistribute":
            raw = self.runtime.exec(
                params.host_name,
                "vtysh -c 'show running-config' 2>/dev/null "
                "| grep -c '^[[:space:]]*redistribute connected' || echo 0",
            ).strip()
            try:
                redist_count = int(raw.splitlines()[-1])
            except (ValueError, IndexError):
                redist_count = -1
            verified = redist_count == 0
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "mode": mode,
                    "redistribute_connected_count": redist_count,
                },
            )

        prefix = params.prefix or self._withdrawn_prefix
        if mode == "prefix" and prefix:
            prefix = str(ipaddress.ip_network(prefix, strict=False))
            cfg = self.runtime.exec(
                params.host_name,
                "vtysh -c 'show running-config' 2>/dev/null",
            )
            # Match BGP network line only (not OSPF ``network … area``).
            present = False
            in_bgp = False
            for line in cfg.splitlines():
                s = line.lstrip()
                if s.startswith("router bgp"):
                    in_bgp = True
                elif s.startswith("router ") and not s.startswith("router bgp"):
                    in_bgp = False
                if in_bgp and (
                    s == f"network {prefix}" or s.startswith(f"network {prefix} ")
                ):
                    present = True
                    break
            verified = not present
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "mode": mode,
                    "prefix": prefix,
                    "network_present": present,
                },
            )

        bgp_count = self._count_running_networks(params.host_name, section="bgp")
        ospf_count = self._count_running_networks(params.host_name, section="ospf")
        verified = bgp_count == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "mode": mode,
                "bgp_network_count": bgp_count,
                "ospf_network_count": ospf_count,
            },
        )

    def _verify_missing_adv_native(
        self, params: BGPMissingAdvertiseParams, ops: RouterFaultOps
    ) -> dict:
        mode = self._inject_mode or (
            "native_prefix" if params.prefix else "native_bgp_network"
        )
        prefix = params.prefix or self._withdrawn_prefix
        if mode == "native_prefix" and prefix:
            prefix = str(ipaddress.ip_network(prefix, strict=False))
            verified = prefix not in set(ops.bgp_networks())
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "mode": mode,
                    "prefix": prefix,
                },
            )
        networks = ops.bgp_networks()
        verified = len(networks) == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "mode": mode,
                "bgp_network_count": len(networks),
                "networks": networks,
            },
        )


# ==================================================================
""" Problem: BGP blackhole community leak (remote-triggered blackholing). """
# ==================================================================


# Operator-style name for the customer prefix the misapplied policy matches.
RTBH_PREFIX_LIST = "CUSTOMER-PREFIXES"


def _profile_leaker(problem: ProblemBase, bgp: dict, host_name: str) -> str:
    """Return the profile leaker; ``host_name`` must name it so GT matches inject."""
    leaker = str(bgp.get("leaker_device") or host_name)
    if host_name != leaker:
        raise RuntimeCapabilityError(
            f"{type(problem).__name__}: host_name={host_name!r} must be the "
            f"profile leaker_device {leaker!r}."
        )
    return leaker


def _session_ip(bgp: dict, local: str, remote: str) -> str:
    """Address of ``remote`` on its BGP session with ``local`` (inventory)."""
    for session in bgp.get("sessions") or []:
        if (
            session.get("local_device") == local
            and session.get("remote_device") == remote
        ):
            return str(session.get("remote_ip") or "")
    return ""


class BGPBlackholeCommunityLeakParams(BaseModel):
    """Parameters for injecting a BGP blackhole community leak fault."""

    host_name: str = Field(
        description="Target router host name for the export-policy fault."
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional data-plane observer for symptom/verify probes.",
    )
    probe_dst_ip: str | None = Field(
        default=None,
        description="Optional ping destination for symptom/verify probes.",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer/stub host near the leaker.",
    )


class BGPBlackholeCommunityLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    root_cause_name: str = "bgp_blackhole_community_leak"
    description = "A provider blackhole BGP community is leaked on export."
    TAGS: str = ["bgp", "isp", "ebgp", "rtbh"]
    COMPATIBLE_COLUMNS = frozenset({"isp_abilene_ebgp_rtbh", "isp_dfn-bwin_ebgp_rtbh"})

    Params = BGPBlackholeCommunityLeakParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = loaded_inventory(ctx.net_env)
        bgp_inv = inventory.get("bgp")
        if not isinstance(bgp_inv, dict) or not bgp_inv.get("rtbh"):
            _plan, bgp_inv = _compile_isp_bgp_inventory(
                ctx.scenario, igp="ospf", bgp_mode="ebgp", rtbh=True
            )
        return dict(isp_inject_params(ctx.problem, inventory, bgp_inv))

    symptom_desc = (
        "Reachability to a legitimately originated business prefix is lost while "
        "BGP sessions remain up."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPBlackholeCommunityLeakParams):
        bgp = self._rtbh_bgp_inventory()
        return [node_resource(_profile_leaker(self, bgp, params.host_name))]

    def _rtbh_bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        if not isinstance(bgp, dict) or not bgp.get("rtbh"):
            raise RuntimeCapabilityError(
                f"{type(self).__name__} requires a named eBGP RTBH scenario."
            )
        return bgp

    def inject_fault(self, params: BGPBlackholeCommunityLeakParams):
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot inject_fault: unsupported backend "
                f"{self.lab_backend!r} (Kathara + FRR only)."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        neighbor_ip = str(bgp.get("leaker_to_rtbh_neighbor_ip") or "")
        if not target_prefix or not community or not route_map or not neighbor_ip:
            raise RuntimeCapabilityError(
                f"{type(self).__name__} missing RTBH inventory fields."
            )
        cmd = frr_config_command(
            f"ip prefix-list {RTBH_PREFIX_LIST} seq 5 permit {target_prefix} le 24",
            f"route-map {route_map} permit 5",
            f"match ip address prefix-list {RTBH_PREFIX_LIST}",
            f"set community {community} additive",
        )
        self.runtime.exec(leaker, cmd)
        frr_save_config(self.runtime, leaker)
        self.runtime.exec(
            leaker,
            f"vtysh -c 'clear ip bgp {neighbor_ip} soft out' 2>/dev/null || true",
        )
        provider = str(bgp.get("rtbh_provider_device") or "")
        if provider:
            wait_until(
                lambda: self._community_present(provider, target_prefix, community),
                20,
            )
        self.logger.info(
            f"Injected BGP blackhole community leak on {leaker} toward "
            f"{neighbor_ip} for {target_prefix} (community {community})."
        )

    def _bgp_sessions_established(self, devices: list[str]) -> bool:
        from nika.net_env.verify import frr_bgp_established_peers

        return all(
            frr_bgp_established_peers(
                self.runtime.exec(
                    device, "vtysh -c 'show bgp summary json' 2>/dev/null || true"
                )
            )
            for device in devices
        )

    def _origin_preserved(
        self,
        *,
        leaker: str,
        origin_device: str,
        target_prefix: str,
        origin_asn: int,
    ) -> bool:
        origin_cfg = self.runtime.exec(
            origin_device, "vtysh -c 'show running-config' 2>/dev/null"
        )
        if f"network {target_prefix}" not in origin_cfg:
            return False
        leaker_bgp = self.runtime.exec(
            leaker,
            f"vtysh -c 'show bgp ipv4 unicast {target_prefix}' 2>/dev/null || true",
        )
        if "Network not in table" in leaker_bgp:
            return False
        if str(origin_asn) not in leaker_bgp:
            return False
        leaker_cfg = self.runtime.exec(
            leaker, "vtysh -c 'show running-config' 2>/dev/null"
        )
        if f"network {target_prefix}" in leaker_cfg:
            return False
        if f"ip route {target_prefix}" in leaker_cfg and "Null0" in leaker_cfg:
            return False
        return True

    def _community_present(
        self, provider: str, target_prefix: str, community: str
    ) -> bool:
        out = self.runtime.exec(
            provider,
            f"vtysh -c 'show bgp ipv4 unicast {target_prefix}' 2>/dev/null || true",
        )
        if "Network not in table" in out:
            return False
        return community in out

    def _rtbh_forwarding_active(
        self, provider: str, target_prefix: str, discard_nh: str
    ) -> bool:
        fib = self.runtime.exec(
            provider,
            f"vtysh -c 'show ip route {target_prefix}' 2>/dev/null || true",
        )
        bgp = self.runtime.exec(
            provider,
            f"vtysh -c 'show bgp ipv4 unicast {target_prefix}' 2>/dev/null || true",
        )
        network = target_prefix.split("/")[0]
        if discard_nh and discard_nh in fib:
            return True
        if "Null0" in fib and network in fib:
            return True
        if discard_nh and discard_nh in bgp:
            return True
        return "blackhole" in fib.lower() and network in fib

    def verify_fault(self, params: BGPBlackholeCommunityLeakParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        origin_device = str(bgp.get("legitimate_origin_device") or "")
        provider = str(bgp.get("rtbh_provider_device") or "")
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        ping_addr = params.probe_dst_ip or str(bgp.get("target_ping_address") or "")
        observer = params.symptom_host or str(bgp.get("data_plane_observer_host") or "")
        discard_nh = str(bgp.get("discard_next_hop") or "")
        origin_asn = int(bgp.get("legitimate_origin_asn") or 0)

        origin_ok = self._origin_preserved(
            leaker=leaker,
            origin_device=origin_device,
            target_prefix=target_prefix,
            origin_asn=origin_asn,
        )
        community_ok = self._community_present(provider, target_prefix, community)
        rtbh_ok = self._rtbh_forwarding_active(provider, target_prefix, discard_nh)
        # Data-plane impact is a symptom; src/nika/validation/symptom probes it.
        sessions_ok = self._bgp_sessions_established(
            [d for d in (leaker, origin_device, provider) if d]
        )

        verified = origin_ok and community_ok and rtbh_ok and sessions_ok
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "leaker_device": leaker,
                "legitimate_origin_device": origin_device,
                "rtbh_provider_device": provider,
                "target_prefix": target_prefix,
                "blackhole_community": community,
                "observer": observer,
                "dst_ip": ping_addr,
                "origin_preserved": origin_ok,
                "community_present": community_ok,
                "rtbh_forwarding_active": rtbh_ok,
                "sessions_ok": sessions_ok,
            },
        )

    def oracle_playbook(
        self, params: BGPBlackholeCommunityLeakParams
    ) -> OraclePlaybook:
        bgp = self._rtbh_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        provider = str(bgp.get("rtbh_provider_device") or "")
        prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        observer = params.symptom_host or str(bgp.get("data_plane_observer_host") or "")
        dst = params.probe_dst_ip or str(bgp.get("target_ping_address") or "")
        leaker_ip = _session_ip(bgp, provider, leaker)
        return OraclePlaybook(
            finding=(
                f"{leaker}'s outbound route-map {route_map} tags {prefix} with the "
                f"provider blackhole community {community}, so {provider} "
                "discards traffic to that prefix."
            ),
            steps=[
                ping_fails(
                    observer, dst, f"Confirm {observer} cannot reach {dst} in {prefix}."
                ),
                shell(
                    provider,
                    vtysh(f"show bgp ipv4 unicast {prefix}"),
                    f"Inspect the BGP paths for {prefix} on {provider}.",
                    contains(
                        rf"from {re.escape(leaker_ip)} [^\n]*\n[^\n]*\bbest\b"
                        rf"[^\n]*\n\s+Community:[^\n]*\b{re.escape(community)}\b",
                        f"the best path is learned from {leaker} ({leaker_ip}) "
                        f"and carries community {community}",
                    ),
                    evidence=True,
                ),
                shell(
                    provider,
                    f"{vtysh(f'show ip route {prefix}')}; ip route get {dst} 2>&1",
                    f"Check how {provider} forwards {prefix}.",
                    contains(
                        r"blackhole|unreachable|Null0|inactive",
                        "traffic to the prefix is discarded",
                    ),
                ),
                shell(
                    leaker,
                    vtysh(f"show bgp ipv4 unicast {prefix}"),
                    f"Check whether {leaker} received {prefix} already tagged.",
                    all_of(
                        contains(r"Paths: \(\d+ available", f"{leaker} holds {prefix}"),
                        lacks(
                            rf"Community:[^\n]*\b{re.escape(community)}\b",
                            f"the path {leaker} received has no {community}",
                        ),
                    ),
                    evidence=True,
                ),
                shell(
                    leaker,
                    vtysh("show running-config")
                    + f" | grep -E '^route-map {route_map} |^ (match|set) |"
                    "^ip prefix-list |route-map .* out$'",
                    f"Inspect {leaker}'s export policy toward {provider}.",
                    contains(
                        rf"^route-map {re.escape(route_map)} permit \d+\n"
                        rf"(?: match [^\n]*\n)* set community [^\n]*"
                        rf"\b{re.escape(community)}\b",
                        f"route-map {route_map} sets {community} on export",
                    ),
                    evidence=True,
                ),
            ],
        )

    def recover_fault(self, params: BGPBlackholeCommunityLeakParams) -> dict:
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot recover_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rtbh_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        route_map = str(bgp.get("leaker_outbound_route_map") or "")
        neighbor_ip = str(bgp.get("leaker_to_rtbh_neighbor_ip") or "")
        target_prefix = str(bgp.get("target_prefix") or "")
        community = str(bgp.get("blackhole_community") or "")
        provider = str(bgp.get("rtbh_provider_device") or "")
        ping_addr = params.probe_dst_ip or str(bgp.get("target_ping_address") or "")
        observer = params.symptom_host or str(bgp.get("data_plane_observer_host") or "")
        discard_nh = str(bgp.get("discard_next_hop") or "")

        cmd = frr_config_command(
            f"no route-map {route_map} permit 5",
            f"no ip prefix-list {RTBH_PREFIX_LIST} seq 5",
        )
        self.runtime.exec(leaker, cmd)
        frr_save_config(self.runtime, leaker)
        self.runtime.exec(
            leaker,
            f"vtysh -c 'clear ip bgp {neighbor_ip} soft out' 2>/dev/null || true",
        )
        wait_until(
            lambda: (
                not self._community_present(provider, target_prefix, community)
                and not self._rtbh_forwarding_active(
                    provider, target_prefix, discard_nh
                )
            ),
            20,
        )

        community_gone = not self._community_present(provider, target_prefix, community)
        rtbh_cleared = not self._rtbh_forwarding_active(
            provider, target_prefix, discard_nh
        )
        dataplane_ok = self.runtime.ping_ok(observer, ping_addr, count=3)
        sessions_ok = self._bgp_sessions_established([leaker, provider])

        ok = community_gone and rtbh_cleared and dataplane_ok and sessions_ok
        details = {
            "host": params.host_name,
            "leaker_device": leaker,
            "observer": observer,
            "dst_ip": ping_addr,
            "community_gone": community_gone,
            "rtbh_cleared": rtbh_cleared,
            "dataplane_ok": dataplane_ok,
            "sessions_ok": sessions_ok,
        }
        self.logger.info(
            f"recover_fault bgp_blackhole_community_leak: ok={ok} {details}"
        )
        return {"verified": ok, "details": details}


# ==================================================================
""" Problem: BGP export-policy misconfig causing RPKI-invalid route leak. """
# ==================================================================


class BGPRPKIInvalidRouteLeakParams(BaseModel):
    """Parameters for injecting an RPKI-invalid BGP route leak."""

    host_name: str = Field(
        description="Primary leaker router from the ISP RPKI profile inventory."
    )


class BGPRPKIInvalidRouteLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_rpki_invalid_route_leak"
    description = "RPKI-invalid prefixes are leaked into BGP."
    TAGS: str = ["rpki"]
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": True}

    Params = BGPRPKIInvalidRouteLeakParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = loaded_inventory(ctx.net_env)
        bgp_inv = inventory.get("bgp")
        if not isinstance(bgp_inv, dict) or not bgp_inv.get("rpki"):
            isp_opts = ctx.isp_options or isp_protocol_for(cls, {"rpki"})
            _plan, bgp_inv = _compile_isp_bgp_inventory(
                ctx.scenario,
                igp=str(isp_opts.get("igp") or "ospf"),
                bgp_mode=str(isp_opts.get("bgp_mode") or "ebgp"),
                rpki=True,
            )
        return dict(isp_inject_params(ctx.problem, inventory, bgp_inv))

    symptom_desc = (
        "Some BGP speakers learn RPKI-invalid routes from a leaked origin while "
        "ROV-enabled peers reject them."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPRPKIInvalidRouteLeakParams):
        return [
            node_resource(device)
            for device in self._leaker_devices(self._rpki_bgp_inventory(), params)
        ]

    def _leaker_devices(
        self, bgp: dict, params: BGPRPKIInvalidRouteLeakParams
    ) -> list[str]:
        """The border router whose eBGP export policy inject rewrites.

        Only its sessions announce the leak; the rest of the leaker AS keeps
        its healthy export policy.
        """
        return [_profile_leaker(self, bgp, params.host_name)]

    def _prefix_learned(self, device: str, prefix: str, origin_asn: int) -> bool:
        """Whether ``device`` holds a path for ``prefix`` originated by ``origin_asn``."""
        out = self.runtime.exec(
            device,
            f"vtysh -c 'show bgp ipv4 unicast {prefix} json' 2>/dev/null || true",
        )
        try:
            paths = (json.loads(out or "{}") or {}).get("paths") or []
        except ValueError:
            return False
        for path in paths:
            tokens = str((path.get("aspath") or {}).get("string") or "").split()
            if tokens and tokens[-1] == str(origin_asn):
                return True
        return False

    def _rpki_bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        if not isinstance(bgp, dict) or not bgp.get("rpki"):
            raise RuntimeCapabilityError(
                f"{type(self).__name__} requires an RPKI ISP scenario "
                "(deploy with: nika env run isp_abilene_ebgp_rpki)."
            )
        return bgp

    def inject_fault(self, params: BGPRPKIInvalidRouteLeakParams):
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot inject_fault: unsupported backend "
                f"{self.lab_backend!r} (Kathara + FRR only)."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        devices = self._leaker_devices(bgp, params)
        # Permit the export-deny prefixes on its eBGP export (replaces the
        # healthy deny seq 5).
        for device in devices:
            cmd = frr_config_command(
                "route-map BGP-OUT permit 5",
                f"match ip address prefix-list {EXPORT_DENY_PREFIX_LIST}",
            )
            self.runtime.exec(device, cmd)
            frr_save_config(self.runtime, device)
            self.runtime.exec(
                device, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
        # Wait for eBGP export to reach the non-ROV observer before verify_fault.
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        non_rov = str(bgp.get("non_rov_observer") or "")
        leaker_asn = int(bgp.get("leaker_asn") or 0)
        if non_rov and prefixes:
            wait_until(
                lambda: all(
                    self._prefix_learned(non_rov, p, leaker_asn) for p in prefixes
                ),
                25,
            )
        self.logger.info(
            f"Injected RPKI-invalid route leak via incorrect BGP export policy "
            f"on {leaker}."
        )

    def verify_fault(self, params: BGPRPKIInvalidRouteLeakParams) -> dict:
        """Verify the leaker AS's live export policy now sends the leak prefixes.

        Reads only the leaker side: the running ``BGP-OUT`` permit on every
        leaker-AS router and the eBGP peers the leaker advertises each prefix
        to. Which remote ASes accept or reject the leak is the symptom.
        """
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot verify_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        devices = self._leaker_devices(bgp, params)
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        ebgp_peers = {
            str(s.get("remote_ip"))
            for s in bgp.get("sessions") or []
            if s.get("local_device") == leaker and s.get("session_type") == "ebgp"
        }
        export_permit = {
            device: "route-map BGP-OUT permit 5"
            in self.runtime.exec(
                device, "vtysh -c 'show running-config' 2>/dev/null || true"
            )
            for device in devices
        }
        advertised_to: dict[str, list[str]] = {}
        for prefix in prefixes:
            out = self.runtime.exec(
                leaker,
                f"vtysh -c 'show bgp ipv4 unicast {prefix} json' 2>/dev/null || true",
            )
            try:
                targets = set((json.loads(out) or {}).get("advertisedTo") or {})
            except ValueError:
                targets = set()
            advertised_to[prefix] = sorted(targets & ebgp_peers)
        verified = (
            bool(prefixes)
            and all(export_permit.values())
            and all(advertised_to.values())
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "leaker_device": leaker,
                "leak_prefixes": prefixes,
                "export_permit": export_permit,
                "advertised_to_ebgp_peers": advertised_to,
            },
        )

    def oracle_playbook(self, params: BGPRPKIInvalidRouteLeakParams) -> OraclePlaybook:
        bgp = self._rpki_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        prefix = str((bgp.get("leak_prefixes") or [""])[0])
        leaker_asn = str(bgp.get("leaker_asn") or "")
        rov = str(bgp.get("rov_observer") or "")
        non_rov = str(bgp.get("non_rov_observer") or "")
        ebgp_peers = [
            str(s.get("remote_ip"))
            for s in bgp.get("sessions") or []
            if s.get("local_device") == leaker and s.get("session_type") == "ebgp"
        ]
        peer_ip = _session_ip(bgp, leaker, non_rov) or (ebgp_peers or [""])[0]
        vrp_net = re.escape(prefix.split("/")[0])
        origin_path = rf"^\s+(?:\d+ )*{re.escape(leaker_asn)}(?: \{{[^}}]*\}})?\s*$"
        return OraclePlaybook(
            finding=(
                f"{leaker}'s eBGP export route-map BGP-OUT permits the "
                f"{EXPORT_DENY_PREFIX_LIST} prefixes, leaking {prefix} with origin "
                f"AS{leaker_asn}, which RPKI marks invalid."
            ),
            steps=[
                shell(
                    non_rov,
                    vtysh(f"show bgp ipv4 unicast {prefix}"),
                    f"Check which origin {non_rov} (no ROV) learns for {prefix}.",
                    contains(
                        origin_path,
                        f"{non_rov} holds a path for {prefix} originated by "
                        f"AS{leaker_asn}",
                    ),
                    evidence=True,
                    attempts=3,
                    interval_sec=10,
                ),
                OracleStep(
                    f"Validate {prefix} against RPKI on the ROV router {rov}.",
                    "frr_get_rpki_status",
                    {"device": rov, "prefix": prefix},
                    all_of(
                        contains(
                            rf"^{vrp_net}\s+\d+\s+-\s+\d+\s+\d+",
                            f"a ROA (VRP) covers {prefix}",
                        ),
                        lacks(
                            rf"^{vrp_net}\s+\d+\s+-\s+\d+\s+{re.escape(leaker_asn)}\b",
                            f"no VRP authorizes AS{leaker_asn}, so its origin is "
                            "RPKI-invalid",
                        ),
                    ),
                    evidence=True,
                ),
                shell(
                    leaker,
                    vtysh(
                        f"show bgp ipv4 unicast neighbors {peer_ip} advertised-routes",
                    )
                    + f" | grep -E '{re.escape(prefix)}|Total' ",
                    f"Check whether {leaker} advertises {prefix} to eBGP peer {peer_ip}.",
                    contains(
                        rf"^\S*\s*{re.escape(prefix)}\s",
                        f"{leaker} announces {prefix} to {peer_ip}",
                    ),
                    evidence=True,
                ),
                shell(
                    leaker,
                    vtysh("show running-config")
                    + " | grep -E '^route-map BGP-OUT |^ match |^ip prefix-list "
                    f"{EXPORT_DENY_PREFIX_LIST} '",
                    f"Inspect {leaker}'s eBGP export policy BGP-OUT.",
                    contains(
                        r"^route-map BGP-OUT permit \d+\n match ip address prefix-list "
                        rf"{re.escape(EXPORT_DENY_PREFIX_LIST)}\b",
                        f"BGP-OUT permits the {EXPORT_DENY_PREFIX_LIST} prefix-list",
                    ),
                    evidence=True,
                ),
            ],
        )

    def _leak_absent(
        self, observers: list[str], prefixes: list[str], leaker_asn: int
    ) -> bool:
        return not any(
            self._prefix_learned(observer, prefix, leaker_asn)
            for observer in observers
            for prefix in prefixes
        )

    def recover_fault(self, params: BGPRPKIInvalidRouteLeakParams) -> dict:
        """Restore healthy BGP-OUT deny for leak prefixes on the leaker AS."""
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot recover_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        bgp = self._rpki_bgp_inventory()
        leaker = _profile_leaker(self, bgp, params.host_name)
        devices = self._leaker_devices(bgp, params)
        prefixes = [str(p) for p in (bgp.get("leak_prefixes") or [])]
        observers = [
            str(bgp.get("rov_observer") or ""),
            str(bgp.get("non_rov_observer") or ""),
        ]
        observers = [o for o in observers if o]

        for device in devices:
            cmd = frr_config_command(
                "route-map BGP-OUT deny 5",
                f"match ip address prefix-list {EXPORT_DENY_PREFIX_LIST}",
            )
            self.runtime.exec(device, cmd)
            frr_save_config(self.runtime, device)
            self.runtime.exec(
                device, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
            )
        leaker_asn = int(bgp.get("leaker_asn") or 0)
        wait_until(lambda: self._leak_absent(observers, prefixes, leaker_asn), 25)
        leak_absent = self._leak_absent(observers, prefixes, leaker_asn)

        ok = leak_absent
        details = {
            "host": params.host_name,
            "leaker_device": leaker,
            "recovered_devices": devices,
            "leak_prefixes": prefixes,
            "leak_absent": leak_absent,
            "observers": observers,
        }
        self.logger.info(
            f"recover_fault bgp_rpki_invalid_route_leak: ok={ok} {details}"
        )
        return {"verified": ok, "details": details}


# ==================================================================
""" Problem: BGP maximum-prefix exceeded (Optus 2023-inspired). """
# ==================================================================

_FLOOD_PREFIX_BASE = "198.19"
# Operator-style name for the de-aggregated more-specifics the peer advertises.
# The policy goes to every BGP router, as a fleet-wide push would, so it does
# not single out the two routers of the faulty session.
_FLOOD_PREFIX_LIST = "TE-MORE-SPECIFICS"


class BGPMaxPrefixExceededParams(BaseModel):
    """Parameters for injecting a BGP maximum-prefix session reset."""

    receiver_name: str = Field(
        description="Router that enforces maximum-prefix on the eBGP session."
    )
    peer_name: str = Field(
        description="eBGP peer that advertises an excessive prefix set."
    )
    neighbor_ip: str | None = Field(
        default=None,
        description="Peer address as seen from the receiver; resolved from inventory when omitted.",
    )
    maximum_prefix: int | None = Field(
        default=None,
        description="Receiver maximum-prefix threshold; derived from current received count when omitted.",
    )
    flood_count: int = Field(
        default=40,
        ge=5,
        description="Number of /24 test prefixes the peer advertises during inject.",
    )


class BGPMaxPrefixExceeded(ProblemBase):
    """An excessive advertisement trips a receiver's terminating prefix limit.

    RFC 4486 section 3 defines Cease subcode 1, Maximum Number of Prefixes
    Reached. The receiver must hold the session down and withdraw its unique
    customer route, rather than merely reject excess routes.
    https://www.rfc-editor.org/rfc/rfc4486#section-3
    """

    root_cause_resource_match = "any"
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_max_prefix_exceeded"
    description = "BGP maximum-prefix limit is exceeded on a session."
    TAGS: str = ["bgp", "isp"]
    COMPATIBLE_COLUMNS = frozenset({"isp_abilene/ebgp", "isp_geant/ebgp"})
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}
    native_router_operations = frozenset(
        {
            "bgp_established_peers",
            "bgp_received_prefix_count",
            "bgp_asn",
            "bgp_prefix_limit",
            "set_bgp_prefix_limit",
            "bgp_prefix_limit_exceeded",
            "bgp_last_notification",
            "permit_bgp_prefixes",
            "advertise_bgp_prefixes",
            "has_bgp_route",
            "bgp_networks",
            "save_config",
            "restore_config",
            "reset_bgp_session",
            "ipv4_addresses",
        }
    )

    Params = BGPMaxPrefixExceededParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        """The session that carries the customer site's route (its only path)."""
        from nika.net_env.isp.inject_targets import customer_route_session

        bgp_inv = loaded_inventory(ctx.net_env).get("bgp")
        if not isinstance(bgp_inv, dict) or "customer_site" not in bgp_inv:
            isp_opts = ctx.isp_options or isp_protocol_for(cls, {"bgp"})
            _plan, bgp_inv = _compile_isp_bgp_inventory(
                ctx.scenario,
                igp=str(isp_opts.get("igp") or "ospf"),
                bgp_mode=str(isp_opts.get("bgp_mode") or "ebgp"),
            )
        params = dict(customer_route_session(bgp_inv))
        params["flood_count"] = "120"
        return params

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        for key in ("receiver_name", "peer_name"):
            require_node_capability(ctx, cls.root_cause_name, inject.get(key), "bgp")

    symptom_desc = (
        "An eBGP peer advertises more prefixes than the receiver's configured "
        "maximum-prefix, resetting the session and withdrawing learned routes; "
        "a customer route learned only over that session leaves the receiver's "
        "AS, and the customer loses its service there."
    )

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        from nika.problems.support.router_ops import profile_fault_operations

        if {"frr_shell", "bgp_asn"} <= profile_fault_operations(device_profile):
            return True
        return super().runs_on_device_profile(device_profile)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._max_prefix_state: dict | None = None

    def root_cause_resources(self, params: BGPMaxPrefixExceededParams):
        return [
            node_resource(params.receiver_name),
            node_resource(params.peer_name),
        ]

    def _frr(self, router: str, command: str) -> str:
        """Run ``command`` beside ``router``'s FRR: on the node, or through its platform."""
        ops = native_fault_ops(self, router, "frr_shell", frr_fallback=True)
        if ops is not None:
            return ops.frr_shell(command)
        if self.lab_backend != "kathara":
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: {router!r} does not route with FRR."
            )
        return self.runtime.exec(router, command)

    def _frr_save(self, router: str) -> None:
        self._frr(router, FRR_SAVE_COMMAND)

    def _bgp_inventory(self) -> dict:
        inventory = getattr(self.net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
        return bgp if isinstance(bgp, dict) else {}

    def _resolve_session(
        self, params: BGPMaxPrefixExceededParams
    ) -> tuple[str, str, str]:
        receiver = params.receiver_name
        peer = params.peer_name
        neighbor_ip = params.neighbor_ip
        if neighbor_ip:
            return receiver, peer, neighbor_ip

        bgp = self._bgp_inventory()
        sessions = [
            s
            for s in (bgp.get("sessions") or [])
            if str(s.get("session_type") or "") == "ebgp"
            and str(s.get("local_device") or "") == receiver
            and str(s.get("remote_device") or "") == peer
            and s.get("remote_ip")
        ]
        if sessions:
            sessions = sorted(sessions, key=lambda s: str(s.get("remote_ip") or ""))
            return receiver, peer, str(sessions[0]["remote_ip"])

        # Fall back to any eBGP session matching the device pair (either direction).
        for s in bgp.get("sessions") or []:
            if str(s.get("session_type") or "") != "ebgp":
                continue
            local = str(s.get("local_device") or "")
            remote = str(s.get("remote_device") or "")
            if {local, remote} == {receiver, peer} and s.get("remote_ip"):
                if local == receiver:
                    return receiver, peer, str(s["remote_ip"])
                if s.get("local_ip"):
                    return receiver, peer, str(s["local_ip"])

        raise RuntimeCapabilityError(
            f"{type(self).__name__} could not resolve eBGP neighbor IP for "
            f"receiver={receiver!r} peer={peer!r}; pass neighbor_ip or deploy "
            "isp with bgp_mode=ebgp."
        )

    def _neighbor_output(self, router: str, neighbor_ip: str) -> str:
        output = self._frr(
            router,
            f"vtysh -c 'show bgp neighbors {neighbor_ip}' 2>/dev/null || true",
        )
        if f"BGP neighbor is {neighbor_ip}," not in output:
            raise ValueError(f"Unreadable BGP neighbor {neighbor_ip} on {router!r}")
        return output

    def _session_established(self, router: str, neighbor_ip: str) -> bool:
        ops = native_fault_ops(self, router, "bgp_established_peers", frr_fallback=True)
        if ops is not None:
            return neighbor_ip in ops.bgp_established_peers()
        out = self._neighbor_output(router, neighbor_ip)
        for line in out.splitlines():
            lower = line.lower()
            if "bgp state" in lower and "established" in lower:
                return True
        return False

    def _received_prefix_count(self, router: str, neighbor_ip: str) -> int:
        ops = native_fault_ops(
            self, router, "bgp_received_prefix_count", frr_fallback=True
        )
        if ops is not None:
            return ops.bgp_received_prefix_count(neighbor_ip)
        out = self._neighbor_output(router, neighbor_ip)
        for pattern in (
            r"Prefix received(?: count)?:\s*(\d+)",
            r"Accepted prefixes?:\s*(\d+)",
            r"prefixes received[:\s]+(\d+)",
        ):
            match = re.search(pattern, out, re.IGNORECASE)
            if match:
                return int(match.group(1))
        summary = self._frr(
            router, "vtysh -c 'show bgp summary json' 2>/dev/null || true"
        )
        peers = json.loads(summary)["ipv4Unicast"]["peers"]
        return int(peers[neighbor_ip]["pfxRcd"])

    def customer_route(self, receiver: str, peer: str) -> dict | None:
        """The scenario's customer route that ``receiver`` learns only from ``peer``."""
        site = self._bgp_inventory().get("customer_site")
        if (
            isinstance(site, dict)
            and site.get("partner") == receiver
            and site.get("provider") == peer
        ):
            return site
        return None

    def _has_route(self, router: str, prefix: str) -> bool:
        ops = native_fault_ops(self, router, "has_bgp_route", frr_fallback=True)
        if ops is not None:
            return ops.has_bgp_route(prefix)
        out = self._frr(
            router, f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true"
        )
        if "Network not in table" in out:
            return False
        paths = re.search(r"Paths:\s*\((\d+) available", out)
        if f"BGP routing table entry for {prefix}" not in out or paths is None:
            raise ValueError(f"Unreadable BGP route {prefix} on {router!r}")
        return int(paths[1]) > 0

    def _business_prefix_missing(self, receiver: str, peer: str) -> bool:
        bgp = self._bgp_inventory()
        peer_prefixes = [
            str(item.get("prefix") or "")
            for item in (bgp.get("originated") or [])
            if str(item.get("device") or "") == peer and item.get("prefix")
        ]
        if not peer_prefixes:
            # Fall back to any originated prefix not owned by the receiver.
            peer_prefixes = [
                str(item.get("prefix") or "")
                for item in (bgp.get("originated") or [])
                if str(item.get("device") or "") != receiver and item.get("prefix")
            ]
        if not peer_prefixes:
            return True
        return any(not self._has_route(receiver, prefix) for prefix in peer_prefixes)

    def _bgp_routers(self, receiver: str, peer: str) -> list[str]:
        """Every router with a BGP session in the inventory."""
        routers = {
            str(s.get("local_device") or "")
            for s in self._bgp_inventory().get("sessions") or []
        }
        return sorted({receiver, peer, *routers} - {""})

    def _flood_policy_maps(self, device: str) -> list[str]:
        config = self._frr(device, "vtysh -c 'show running-config'")
        maps = sorted(
            set(
                re.findall(
                    r"^\s*neighbor \S+ route-map ([A-Za-z0-9_.-]+) (?:in|out)$",
                    config,
                    re.M,
                )
            )
        )
        if not maps:
            raise RuntimeCapabilityError(
                "BGP flood setup requires bound import/export policies"
            )
        return maps

    def _install_flood_policy(self, device: str) -> None:
        ops = native_fault_ops(self, device, "permit_bgp_prefixes", frr_fallback=True)
        if ops is not None:
            ops.permit_bgp_prefixes("198.19.0.0/16", 24)
            return
        # Include solved import policies, not only the baseline policy names.
        lines = [
            f"ip prefix-list {_FLOOD_PREFIX_LIST} seq 5 permit 198.19.0.0/16 le 24"
        ]
        for policy in self._flood_policy_maps(device):
            lines += [
                f"route-map {policy} permit 1",
                f"match ip address prefix-list {_FLOOD_PREFIX_LIST}",
            ]
        self._frr(device, frr_config_command(*lines))
        self._frr_save(device)

    def _remove_flood_policy(self, device: str) -> None:
        lines = [
            f"no route-map {policy} permit 1"
            for policy in self._flood_policy_maps(device)
        ]
        lines.append(f"no ip prefix-list {_FLOOD_PREFIX_LIST}")
        self._frr(device, frr_config_command(*lines))
        self._frr_save(device)

    def _flood_prefixes(self, count: int, *, peer: str, neighbor_ip: str) -> list[str]:
        """Pick ``count`` /24s scattered within the flood /16, not a tidy block.

        Real de-aggregation/leak incidents (e.g. Optus 2023) announce many
        non-contiguous more-specifics, not one ascending sequential range.
        The selection is deterministic per session (seeded from the peer and
        neighbor address) so repeated runs of the same case see the same
        flood set.
        """
        rng = random.Random(f"{peer}|{neighbor_ip}")
        octets = list(range(256))
        rng.shuffle(octets)
        count = min(count, 256)
        return sorted(f"{_FLOOD_PREFIX_BASE}.{octet}.0/24" for octet in octets[:count])

    def _advertise_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        ops = native_fault_ops(self, peer, "advertise_bgp_prefixes", frr_fallback=True)
        if ops is not None:
            ops.advertise_bgp_prefixes(prefixes)
            return
        lines: list[str] = []
        config = self._frr(peer, "vtysh -c 'show running-config'")
        if "no bgp network import-check" not in config:
            # FRR originates a ``network`` only when a matching route exists,
            # so back the announcements with discard routes under import check.
            lines.extend(f"ip route {prefix} Null0" for prefix in prefixes)
        lines += [f"router bgp {asn}", "address-family ipv4 unicast"]
        lines.extend(f"network {prefix}" for prefix in prefixes)
        self._frr(peer, frr_config_command(*lines))
        self._frr_save(peer)
        self._frr(peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true")

    def _withdraw_flood(self, peer: str, asn: int, prefixes: list[str]) -> None:
        lines = [f"router bgp {asn}", "address-family ipv4 unicast"]
        lines.extend(f"no network {prefix}" for prefix in prefixes)
        config = self._frr(peer, "vtysh -c 'show running-config'")
        statics = [p for p in prefixes if f"ip route {p} Null0" in config]
        if statics:
            lines += ["exit-address-family", "exit"]
            lines.extend(f"no ip route {prefix} Null0" for prefix in statics)
        self._frr(peer, frr_config_command(*lines))
        self._frr_save(peer)

    def _set_maximum_prefix(
        self, receiver: str, asn: int, neighbor_ip: str, limit: int
    ) -> None:
        ops = native_fault_ops(
            self, receiver, "set_bgp_prefix_limit", frr_fallback=True
        )
        if ops is not None:
            ops.set_bgp_prefix_limit(neighbor_ip, limit)
            return
        cmd = frr_config_command(
            f"router bgp {asn}",
            "address-family ipv4 unicast",
            f"neighbor {neighbor_ip} maximum-prefix {limit}",
        )
        self._frr(receiver, cmd)
        self._frr_save(receiver)

    def _clear_maximum_prefix(self, receiver: str, asn: int, neighbor_ip: str) -> None:
        cmd = frr_config_command(
            f"router bgp {asn}",
            "address-family ipv4 unicast",
            f"no neighbor {neighbor_ip} maximum-prefix",
        )
        self._frr(receiver, cmd)
        self._frr_save(receiver)

    def _wait_session_state(
        self,
        router: str,
        neighbor_ip: str,
        *,
        established: bool,
        timeout_s: float = 90.0,
        poll_s: float = 2.0,
    ) -> bool:
        return wait_until(
            lambda: self._session_established(router, neighbor_ip) == established,
            timeout_s,
            interval_s=poll_s,
        )

    def inject_fault(self, params: BGPMaxPrefixExceededParams):
        receiver, peer, neighbor_ip = self._resolve_session(params)
        self._saved_native_configs = {}
        self._native_peer_baseline = {}
        for host in (receiver, peer):
            ops = native_fault_ops(self, host, "save_config", frr_fallback=True)
            if ops is not None:
                self._saved_native_configs[host] = ops.save_config()
                self._native_peer_baseline[host] = ops.bgp_established_peers()
        if self._saved_native_configs and set(self._saved_native_configs) != {
            receiver,
            peer,
        }:
            raise RuntimeCapabilityError(
                "BGP maximum-prefix requires both session endpoints to use native profiles or FRR"
            )
        if self._saved_native_configs and self.customer_route(receiver, peer) is None:
            raise RuntimeCapabilityError(
                "BGP maximum-prefix requires the unique customer route in the deployed inventory"
            )
        if not self._session_established(receiver, neighbor_ip):
            # Allow BGP to finish converging after deploy.
            if not self._wait_session_state(
                receiver, neighbor_ip, established=True, timeout_s=180.0
            ):
                raise RuntimeError(
                    f"eBGP session {receiver}←{neighbor_ip} ({peer}) not Established "
                    "before maximum-prefix inject."
                )

        received = self._received_prefix_count(receiver, neighbor_ip)
        limit = params.maximum_prefix
        if limit is None:
            limit = max(received + 5, 10)
        flood_count = max(params.flood_count, limit + 15)
        prefixes = self._flood_prefixes(flood_count, peer=peer, neighbor_ip=neighbor_ip)

        receiver_asn = self._asn(receiver)
        peer_asn = self._asn(peer)

        self._set_maximum_prefix(receiver, receiver_asn, neighbor_ip, limit)
        # Confirm session still up with received < limit before the flood.
        if not self._session_established(receiver, neighbor_ip):
            raise RuntimeError(
                "maximum-prefix configuration unexpectedly reset the eBGP session "
                "before the peer flood."
            )
        if self._received_prefix_count(receiver, neighbor_ip) >= limit:
            raise RuntimeError(
                f"received prefixes already >= maximum-prefix {limit} before flood."
            )

        routers = self._bgp_routers(receiver, peer)
        # Native routers other than the endpoints get the flood permit too;
        # recovery restores their saved configuration.
        for host in routers:
            ops = native_fault_ops(self, host, "save_config")
            if ops is not None and host not in self._saved_native_configs:
                self._saved_native_configs[host] = ops.save_config()
        bounded_parallel_map(self._install_flood_policy, routers)
        self._advertise_flood(peer, peer_asn, prefixes)

        self._wait_session_state(
            receiver, neighbor_ip, established=False, timeout_s=90.0
        )

        if self._session_established(receiver, neighbor_ip):
            # Force a refresh if FRR has not yet applied the limit.
            if not self._saved_native_configs:
                self._frr(
                    peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true"
                )
            self._wait_session_state(
                receiver, neighbor_ip, established=False, timeout_s=60.0
            )

        self._max_prefix_state = {
            "receiver": receiver,
            "peer": peer,
            "neighbor_ip": neighbor_ip,
            "receiver_asn": receiver_asn,
            "peer_asn": peer_asn,
            "limit": limit,
            "prefixes": prefixes,
        }
        self.logger.info(
            f"Injected bgp_max_prefix_exceeded: peer={peer} flooded "
            f"{len(prefixes)} prefixes toward receiver={receiver} "
            f"neighbor={neighbor_ip} maximum-prefix={limit}."
        )

    def verify_fault(self, params: BGPMaxPrefixExceededParams) -> dict:
        """Verify the live limit, the peer's larger prefix set, and the torn-down session.

        The limit and the flood are read from both routers' running configs;
        the receiver must hold the session down for the exceeded limit.
        """
        receiver, peer, neighbor_ip = self._resolve_session(params)
        ops = native_fault_ops(self, receiver, "bgp_prefix_limit", frr_fallback=True)
        if ops is not None:
            limit = ops.bgp_prefix_limit(neighbor_ip)
            held_for_limit = ops.bgp_prefix_limit_exceeded(neighbor_ip)
        else:
            receiver_cfg = self._frr(
                receiver, "vtysh -c 'show running-config' 2>/dev/null || true"
            )
            match = re.search(
                rf"neighbor {re.escape(neighbor_ip)} maximum-prefix (\d+)", receiver_cfg
            )
            limit = int(match.group(1)) if match else None
            neighbor = self._neighbor_output(receiver, neighbor_ip)
            held_for_limit = (
                "peer had exceeded the max. no. of prefixes" in neighbor.lower()
            )
        flood = len(self._discover_flood_prefixes(peer))
        session_down = not self._session_established(receiver, neighbor_ip)
        verified = (
            limit is not None and flood > limit and session_down and held_for_limit
        )
        details = {
            "receiver": receiver,
            "peer": peer,
            "neighbor_ip": neighbor_ip,
            "maximum_prefix": limit,
            "peer_flood_prefixes": flood,
            "session_down": session_down,
            "held_down_for_limit": held_for_limit,
        }
        site = self.customer_route(receiver, peer)
        if site is not None:
            # The customer route had no other way into the receiver's AS.
            stranded = not self._has_route(receiver, str(site["prefix"]))
            details["customer_prefix"] = site["prefix"]
            details["customer_route_withdrawn"] = stranded
            verified = verified and stranded
        return build_verify_result(
            fault_type=self.root_cause_name, verified=verified, details=details
        )

    def oracle_playbook(self, params: BGPMaxPrefixExceededParams) -> OraclePlaybook:
        receiver, peer, neighbor_ip = self._resolve_session(params)
        from nika.problems.support.router_ops import native_router_profile

        if native_router_profile(self.net_env, receiver):
            return OraclePlaybook(
                finding=(
                    f"{receiver} holds its session to {peer} down because the "
                    "peer exceeded its terminating prefix limit."
                ),
                steps=[
                    router_cli(
                        self.net_env,
                        receiver,
                        f"Read the prefix limit on {receiver}'s session to {peer}.",
                        contains(
                            r"maximum-prefix \d+|input.limit-process-routes-ipv4=\d+",
                            "the receiver configures a terminating prefix limit",
                        ),
                        frr=vtysh("show running-config"),
                        iosxr_command="show running-config router bgp",
                        routeros_command=(
                            "/routing/bgp/connection/print terse where "
                            f'remote.address="{neighbor_ip}"'
                        ),
                        evidence=True,
                    ),
                    router_cli(
                        self.net_env,
                        receiver,
                        f"Read why {receiver} holds the session to {peer} down.",
                        all_of(
                            contains(
                                r"peer had exceeded the max\. no\. of prefixes configured"
                                r"|^(?=[^\n]*remote\.address=)[^\n]*"
                                r"(?:^|\s)limit-exceeded(?:\s|$)",
                                "the session reports prefix overflow, distinguishing "
                                "it from authentication, TTL, and ACL failures",
                            ),
                            contains(
                                r"BGP state = Idle\b|^prefix-limit-established=0\s*$",
                                "the session is down now, rather than carrying a historical reset reason",
                            ),
                        ),
                        frr=vtysh(f"show bgp neighbors {neighbor_ip}"),
                        iosxr_command=f"show bgp neighbor {neighbor_ip}",
                        routeros_command=(
                            "/routing/bgp/session/print terse where "
                            f'remote.address="{neighbor_ip}"; '
                            ':put ("prefix-limit-established=" . '
                            "[/routing bgp session print count-only where "
                            f'remote.address="{neighbor_ip}" established=yes])'
                        ),
                        evidence=True,
                        attempts=12,
                        interval_sec=10,
                    ),
                    router_cli(
                        self.net_env,
                        peer,
                        f"Inspect the routes {peer} originates toward {receiver}.",
                        frr=vtysh("show bgp ipv4 unicast"),
                        iosxr_command="show bgp ipv4 unicast",
                        routeros_command="/ip/firewall/address-list/print terse",
                    ),
                ],
            )

        def peer_count(outputs) -> dict:
            match = re.search(
                r"Maximum prefixes allowed (\d+)", outputs.get("neighbor", "")
            )
            limit = match.group(1) if match else "0"
            # Count the routes the peer originates itself (next hop 0.0.0.0).
            command = (
                "n=$(vtysh -c 'show bgp ipv4 unicast' | grep -cE ' 0\\.0\\.0\\.0 '); "
                f'echo "originated=$n receiver_limit={limit}"; '
                f'[ "$n" -gt {limit} ] && echo EXCEEDS_LIMIT || true'
            )
            return {"host_name": peer, "command": command, "timeout": 20}

        steps = [
            shell(
                receiver,
                vtysh("show bgp summary") + " | tail -n +6",
                f"List the BGP sessions of {receiver}.",
                contains(
                    rf"^{re.escape(neighbor_ip)}\s.*\bIdle\b",
                    f"the eBGP session to {neighbor_ip} ({peer}) is down",
                ),
            ),
            shell(
                receiver,
                vtysh(f"show bgp neighbors {neighbor_ip}")
                + " | grep -E 'BGP state|Maximum prefixes|Last reset|"
                "exceeded the max|accepted prefixes'",
                f"Inspect why {receiver} holds the session to {peer} down.",
                all_of(
                    contains(r"BGP state = Idle", "the session is Idle"),
                    contains(
                        r"Maximum prefixes allowed \d+",
                        "a maximum-prefix limit is configured",
                    ),
                    contains(
                        r"exceeded the max\. no\. of prefixes",
                        f"{receiver} shut the session because {peer} exceeded it",
                    ),
                ),
                evidence=True,
                save_as="neighbor",
            ),
            OracleStep(
                f"Count the prefixes {peer} originates toward {receiver}.",
                "exec_shell",
                peer_count,
                contains(
                    r"^EXCEEDS_LIMIT$",
                    f"{peer} originates more prefixes than {receiver}'s limit",
                ),
                evidence=True,
            ),
        ]
        site = self.customer_route(receiver, peer)
        if site is not None:
            steps.append(
                shell(
                    receiver,
                    vtysh(f"show bgp ipv4 unicast {site['prefix']}"),
                    f"Check the customer route {site['prefix']} that only {peer} "
                    "provides.",
                    contains(r"Network not in table", "the customer route is gone"),
                )
            )
        return OraclePlaybook(
            finding=(
                f"{peer} advertises more prefixes than the maximum-prefix limit "
                f"{receiver} enforces on their eBGP session, so {receiver} tore the "
                "session down and withdrew every route learned over it."
            ),
            steps=steps,
        )

    def _discover_flood_prefixes(self, peer: str) -> list[str]:
        ops = native_fault_ops(self, peer, "bgp_networks", frr_fallback=True)
        if ops is not None:
            return sorted(
                p
                for p in ops.bgp_networks()
                if (network := ipaddress.ip_network(p)).prefixlen == 24
                and network.subnet_of(ipaddress.ip_network("198.19.0.0/16"))
            )
        conf = self._frr(peer, "vtysh -c 'show running-config' 2>/dev/null || true")
        found = re.findall(
            rf"network\s+({re.escape(_FLOOD_PREFIX_BASE)}\.\d+\.0/24)", conf
        )
        if found:
            return sorted(set(found))
        return []

    def _asn(self, host: str) -> int:
        ops = native_fault_ops(self, host, "bgp_asn", frr_fallback=True)
        return (
            ops.bgp_asn()
            if ops is not None
            else self.runtime.frr_get_bgp_asn_number(host)
        )

    def recover_fault(self, params: BGPMaxPrefixExceededParams) -> dict:
        """Withdraw flood prefixes, remove max-prefix, restore eBGP convergence."""
        state = self._max_prefix_state or {}
        receiver, peer, neighbor_ip = self._resolve_session(params)
        receiver = str(state.get("receiver") or receiver)
        peer = str(state.get("peer") or peer)
        neighbor_ip = str(state.get("neighbor_ip") or neighbor_ip)
        saved = getattr(self, "_saved_native_configs", {})
        if saved:
            for host, config in saved.items():
                ops = native_fault_ops(self, host, "restore_config", frr_fallback=True)
                ops.restore_config(config)
            ops = native_fault_ops(
                self, receiver, "reset_bgp_session", frr_fallback=True
            )
            ops.reset_bgp_session(neighbor_ip)

            def sessions_back():
                for host, peers in self._native_peer_baseline.items():
                    ops = native_fault_ops(
                        self, host, "bgp_established_peers", frr_fallback=True
                    )
                    if not peers <= ops.bgp_established_peers():
                        return False
                return self._session_established(receiver, neighbor_ip)

            session_up = wait_until(sessions_back, 180)
            site = self.customer_route(receiver, peer)

            def restored():
                return (
                    site is not None
                    and self._has_route(receiver, str(site["prefix"]))
                    and not self._business_prefix_missing(receiver, peer)
                )

            routes_ok = wait_until(restored, 60) if session_up else False
            return {
                "verified": session_up and routes_ok,
                "details": {
                    "receiver": receiver,
                    "peer": peer,
                    "session_established": session_up,
                    "business_routes_restored": routes_ok,
                    "withdrawn_prefixes": len(state.get("prefixes", [])),
                },
            }
        prefixes = list(state.get("prefixes") or [])
        if not prefixes:
            prefixes = self._discover_flood_prefixes(peer)
        if not prefixes:
            prefixes = self._flood_prefixes(
                max(params.flood_count, 80), peer=peer, neighbor_ip=neighbor_ip
            )
        receiver_asn = int(state.get("receiver_asn") or self._asn(receiver))
        peer_asn = int(state.get("peer_asn") or self._asn(peer))

        self._withdraw_flood(peer, peer_asn, prefixes)
        bounded_parallel_map(
            self._remove_flood_policy, self._bgp_routers(receiver, peer)
        )
        self._clear_maximum_prefix(receiver, receiver_asn, neighbor_ip)
        self._frr(
            receiver,
            f"vtysh -c 'clear ip bgp {neighbor_ip}' 2>/dev/null || true",
        )
        self._frr(peer, "vtysh -c 'clear ip bgp * soft out' 2>/dev/null || true")

        session_up = self._wait_session_state(
            receiver, neighbor_ip, established=True, timeout_s=120.0
        )
        site = self.customer_route(receiver, peer)

        def routes_back() -> bool:
            if site is not None and not self._has_route(receiver, str(site["prefix"])):
                return False
            return not self._business_prefix_missing(receiver, peer)

        routes_ok = routes_back()
        # Business routes may need a short extra settle after session up.
        if session_up and not routes_ok:
            routes_ok = wait_until(routes_back, 15)
        ok = session_up and routes_ok
        details = {
            "receiver": receiver,
            "peer": peer,
            "neighbor_ip": neighbor_ip,
            "session_established": session_up,
            "business_routes_restored": routes_ok,
            "withdrawn_prefixes": len(prefixes),
        }
        self.logger.info(f"recover_fault bgp_max_prefix_exceeded: ok={ok} {details}")
        self._max_prefix_state = None
        return {"verified": ok, "details": details}


# ==================================================================
""" Problem: one-sided BGP TTL security (GTSM). """
# ==================================================================

_BGP_BLOCK_RE = re.compile(r"^router bgp \d+(?: vrf \S+)?$")
_NEIGHBOR_RE = re.compile(
    r"^\s+neighbor (\S+) (?:interface (?:v6only )?)?remote-as \S+"
)
GTSM_HOPS = 1


def bgp_neighbors_by_instance(running_config: str) -> dict[str, list[str]]:
    """``router bgp`` block line -> neighbors (IP, interface, or peer group) with a remote AS."""
    blocks: dict[str, list[str]] = {}
    block = None
    for line in running_config.splitlines():
        if _BGP_BLOCK_RE.match(line):
            block = line
            blocks.setdefault(block, [])
        elif line and not line[0].isspace():
            block = None
        elif block and (match := _NEIGHBOR_RE.match(line)):
            if match[1] not in blocks[block]:
                blocks[block].append(match[1])
    return {key: value for key, value in blocks.items() if value}


class BGPTtlSecurityMismatchParams(BaseModel):
    """Parameters for injecting a one-sided BGP TTL security setting."""

    host_name: str = Field(description="Target router host name.")


class BGPTtlSecurityMismatch(ProblemBase):
    """TTL security (RFC 5082 GTSM) on one router's BGP neighbors only.

    FRR then drops every BGP packet whose TTL is below 255. Its eBGP
    peers still send with TTL 1, and iBGP packets from peers more than one
    hop away arrive below 255, so those sessions never reach Established;
    a directly connected iBGP peer still gets through. Nothing filters TCP
    179: the kernel drops the packets through the socket's minimum TTL. Native
    providers enable the platform's receive-TTL check for eBGP. See
    the AWS Direct Connect note on TTL security:
    https://docs.aws.amazon.com/directconnect/latest/UserGuide/ts-ttl-security.html
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "bgp_ttl_security_mismatch"
    description = "BGP TTL security is configured on one side of the sessions only."
    effect_protocol = "bgp"
    TAGS = ["bgp"]
    # Route-reflector ISP labs peer over iBGP, where directly connected peers
    # keep their sessions, so the router may lose none of its routes.
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}
    supported_backends = None
    native_router_operations = frozenset(
        {
            "enable_bgp_ttl_security",
            "bgp_established_peers",
            "ipv4_addresses",
            "bgp_ttl_security_enabled",
            "save_config",
            "restore_config",
            "reset_bgp",
        }
    )
    Params = BGPTtlSecurityMismatchParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        # GTSM protects external sessions; prefer router0 when it has one.
        ebgp = [r for r in ctx.routers if declared_ebgp(ctx.net_env, r) is not False]
        host = ctx.router0 if ctx.router0 in ebgp else next(iter(ebgp), ctx.router0)
        return {"host_name": host}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        host = inject.get("host_name")
        require_node_capability(ctx, cls.root_cause_name, host, "bgp")
        if declared_ebgp(ctx.net_env, host) is False:
            raise ValueError(f"{cls.root_cause_name} needs a router with eBGP sessions")

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPTtlSecurityMismatchParams):
        return [node_resource(params.host_name)]

    def _neighbors(self, host: str) -> dict[str, list[str]]:
        return bgp_neighbors_by_instance(
            self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        )

    def _apply(self, host: str, *, enable: bool) -> None:
        prefix = "" if enable else "no "
        for block, neighbors in self._neighbors(host).items():
            self.runtime.exec(
                host,
                frr_config_command(
                    block,
                    *(
                        f"{prefix}neighbor {neighbor} ttl-security hops {GTSM_HOPS}"
                        for neighbor in neighbors
                    ),
                ),
            )
        frr_save_config(self.runtime, host)
        # Sessions that are already up keep running until they reset.
        self.runtime.exec(
            host,
            "vtysh -c 'clear bgp *' -c 'clear bgp vrf all *' >/dev/null 2>&1",
        )

    def _secured(self, host: str) -> tuple[list[str], list[str]]:
        config = self.runtime.exec(host, "vtysh -c 'show running-config' 2>/dev/null")
        neighbors = sorted(
            {n for group in bgp_neighbors_by_instance(config).values() for n in group}
        )
        secured = sorted(
            set(
                re.findall(
                    rf"^\s+neighbor (\S+) ttl-security hops {GTSM_HOPS}$", config, re.M
                )
            )
        )
        return neighbors, secured

    def _established(self, host: str) -> set[str]:
        from nika.validation.symptom.routing_probes import bgp_neighbors

        ops = native_fault_ops(self, host, "bgp_established_peers")
        if ops is not None:
            return ops.bgp_established_peers()
        peers = bgp_neighbors(self.runtime, host)
        if peers is None:
            raise RuntimeCapabilityError(f"Unreadable BGP sessions on {host!r}.")
        return {peer for peer, row in peers.items() if row["state"] == "Established"}

    def inject_fault(self, params: BGPTtlSecurityMismatchParams):
        self._healthy_peers = self._established(params.host_name)
        if not self._healthy_peers:
            raise RuntimeCapabilityError(
                "BGP TTL security needs an established session."
            )
        ops = native_fault_ops(self, params.host_name, "enable_bgp_ttl_security")
        if ops is not None:
            self._saved_config = ops.save_config()
            ops.enable_bgp_ttl_security()
            return
        if not self._neighbors(params.host_name):
            raise RuntimeCapabilityError(
                f"{type(self).__name__}: {params.host_name!r} has no BGP neighbors."
            )
        self._apply(params.host_name, enable=True)
        self.logger.info(
            f"Injected BGP TTL security (hops {GTSM_HOPS}) on {params.host_name}."
        )

    def verify_fault(self, params: BGPTtlSecurityMismatchParams) -> dict:
        ops = native_fault_ops(self, params.host_name, "bgp_ttl_security_enabled")
        if ops is not None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=ops.bgp_ttl_security_enabled(),
                details={"host": params.host_name, "profile": ops.profile},
            )
        neighbors, secured = self._secured(params.host_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(neighbors) and set(neighbors) <= set(secured),
            details={
                "host": params.host_name,
                "neighbors": neighbors,
                "ttl_security": secured,
            },
        )

    def oracle_playbook(self, params: BGPTtlSecurityMismatchParams) -> OraclePlaybook:
        host = params.host_name
        peers = _oracle_bgp_peers(self.net_env, host)
        if not peers:
            raise RuntimeCapabilityError(f"No BGP peer declared for {host}.")
        steps = [
            router_cli(
                self.net_env,
                host,
                f"Read {host}'s receive TTL requirement for BGP.",
                contains(
                    r"^\s+(?:neighbor \S+ )?ttl-security(?: hops 1)?\s*$",
                    "the target requires GTSM receive TTL, distinguishing it from "
                    "an MD5 password or TCP port filter",
                ),
                frr=vtysh("show running-config"),
                iosxr_command="show running-config router bgp",
                evidence=True,
            ),
            router_cli(
                self.net_env,
                host,
                f"Inspect BGP session states on {host} after the reset.",
                contains(
                    r"BGP state = (?:Idle|Active|Connect|OpenSent|OpenConfirm)\b",
                    "at least one BGP session cannot establish under the TTL requirement",
                ),
                frr=vtysh("show bgp neighbors"),
                iosxr_command="show bgp neighbors",
                evidence=True,
                attempts=18,
                interval_sec=10,
            ),
        ]
        for peer in peers:
            steps.append(
                router_cli(
                    self.net_env,
                    peer,
                    f"Compare {peer}'s BGP TTL settings with {host}'s requirement.",
                    Check(
                        "the peer has BGP neighbors but no GTSM or multihop TTL "
                        "setting, proving the receive TTL requirement is one-sided",
                        lambda o: (
                            _oracle_bgp_config(o)
                            and not re.search(r"ttl-security|ebgp-multihop", o)
                        ),
                    ),
                    frr=vtysh("show running-config"),
                    iosxr_command="show running-config router bgp",
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=f"{host} requires GTSM TTL on BGP sessions whose peers do not enable it.",
            steps=steps,
        )

    def recover_fault(self, params: BGPTtlSecurityMismatchParams) -> dict:
        baseline = getattr(self, "_healthy_peers", None)
        if not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        ops = native_fault_ops(self, params.host_name, "restore_config")
        if ops is not None:
            saved = getattr(self, "_saved_config", None)
            if saved:
                ops.restore_config(saved)
            reset = native_fault_ops(self, params.host_name, "reset_bgp")
            if reset is not None:
                reset.reset_bgp()
            restored = bool(saved) and wait_until(
                lambda: baseline <= self._established(params.host_name),
                180,
                interval_s=3,
            )
            return {
                "verified": restored and not ops.bgp_ttl_security_enabled(),
                "details": {"host": params.host_name},
            }
        self._apply(params.host_name, enable=False)
        restored = wait_until(
            lambda: baseline <= self._established(params.host_name),
            180,
            interval_s=3,
        )
        neighbors, secured = self._secured(params.host_name)
        return {
            "verified": restored and not secured,
            "details": {"host": params.host_name, "neighbors": neighbors},
        }


# ==================================================================
""" Problem: BGP sessions left without a usable route policy. """
# ==================================================================


def declared_ebgp(net_env, router: str) -> bool | None:
    """Whether ``router`` has an eBGP session, or ``None`` when nothing declares it.

    The BGP inventory answers first; otherwise the router's FRR design,
    including a converted router's, does.
    """
    from nika.net_env.native_routers import router_sketches

    bgp = loaded_inventory(net_env).get("bgp")
    sessions = bgp.get("sessions") if isinstance(bgp, dict) else None
    if sessions:
        return any(
            s.get("local_device") == router and s.get("session_type") == "ebgp"
            for s in sessions
        )
    if getattr(net_env, "lab", None) is None:
        return None
    sketch = router_sketches(net_env).get(router)
    if sketch is None:
        return None
    return _config_has_ebgp(sketch[0])


def _config_has_ebgp(config: str) -> bool:
    """Whether an FRR configuration has an eBGP neighbor."""
    local = re.search(r"^router bgp (\d+)\s*$", config, re.MULTILINE)
    remotes = re.findall(r"^\s+neighbor \S+ remote-as (\S+)", config, re.MULTILINE)
    return bool(local) and any(
        remote == "external" or (remote.isdigit() and remote != local[1])
        for remote in remotes
    )


class BGPSessionPolicyMissingParams(BaseModel):
    """Parameters for leaving a native router's eBGP sessions without a policy."""

    host_name: str = Field(description="Target IOS XR or RouterOS router.")


class BGPSessionPolicyMissing(ProblemBase):
    """The router's eBGP sessions have no route policy it can apply.

    On IOS XR, eBGP neighbors lose their ``route-policy`` and XR applies its
    RFC 8212 default, which drops every route in both directions. On
    RouterOS, the BGP connections name filter chains that no rule defines,
    and RouterOS rejects every route such a chain sees. The sessions stay
    Established in both cases. See RFC 8212:
    https://www.rfc-editor.org/rfc/rfc8212
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "bgp_session_policy_missing"
    description = (
        "BGP sessions have no usable route policy, so the router drops their routes."
    )
    effect_protocol = "bgp"
    TAGS = ["bgp"]
    COMPATIBLE_COLUMNS = frozenset(
        {"dc_clos", "enterprise_branch"}
        | {f"{scenario}/ebgp" for scenario in list_isp_base_scenarios()}
    )
    supported_backends = ("kathara",)
    native_router_operations = frozenset(
        {
            "drop_bgp_session_policy",
            "bgp_session_policy_dropped",
            "bgp_established_peers",
            "remote_bgp_prefixes",
            "save_config",
            "restore_config",
        }
    )
    Params = BGPSessionPolicyMissingParams
    isp_protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        # FRR runs without ebgp-requires-policy in the current environments.
        from nika.problems.support.router_ops import router_platform_has_provider

        return (
            router_platform_has_provider(device_profile)
            and cls.runs_on_device_profile(device_profile)
            and super().is_compatible(target, device_profile, backend=backend)
        )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.problems.support.router_ops import native_router_profile

        native = [
            router
            for router in ctx.routers
            if native_router_profile(ctx.net_env, router)
            and declared_ebgp(ctx.net_env, router)
            and attached_end_hosts(ctx.net_env, router)
        ]
        return {"host_name": choice(ctx.rng, native, ctx.router0)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.router_ops import native_router_profile

        host = inject.get("host_name")
        # The defaults that turn a missing policy into dropped routes are the
        # native platform's; FRR here runs without ``ebgp-requires-policy``.
        if not native_router_profile(ctx.net_env, host):
            raise ValueError(f"{cls.root_cause_name} needs a native router target")
        if not declared_ebgp(ctx.net_env, host):
            raise ValueError(f"{cls.root_cause_name} needs a router with eBGP sessions")
        if not attached_end_hosts(ctx.net_env, host):
            raise ValueError(f"{cls.root_cause_name} needs a router with end hosts")

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: BGPSessionPolicyMissingParams):
        return [node_resource(params.host_name)]

    def _native(self, ops: RouterFaultOps | None, host: str) -> RouterFaultOps:
        if ops is None:
            raise RuntimeCapabilityError(
                f"{type(self).__name__} needs an IOS XR or RouterOS router; "
                f"{host!r} is not one."
            )
        return ops

    def inject_fault(self, params: BGPSessionPolicyMissingParams):
        from nika.problems.support.router_ops import native_fault_ops

        host = params.host_name
        ops = self._native(
            native_fault_ops(self, host, "drop_bgp_session_policy"), host
        )
        self._saved_config = ops.save_config()
        self._healthy_peers = ops.bgp_established_peers()
        self._healthy_routes = set(ops.remote_bgp_prefixes())
        if not self._healthy_peers:
            raise RuntimeCapabilityError(
                "BGP session policy needs an established session."
            )
        ops.drop_bgp_session_policy()
        self.logger.info(f"Dropped eBGP session policies on {params.host_name}.")

    def verify_fault(self, params: BGPSessionPolicyMissingParams) -> dict:
        from nika.problems.support.router_ops import native_fault_ops

        host = params.host_name
        ops = self._native(
            native_fault_ops(self, host, "bgp_session_policy_dropped"), host
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=ops.bgp_session_policy_dropped(),
            details={"host": params.host_name, "profile": ops.profile},
        )

    def oracle_playbook(self, params: BGPSessionPolicyMissingParams) -> OraclePlaybook:
        from nika.problems.support.router_ops import native_router_profile

        host = params.host_name
        profile = native_router_profile(self.net_env, host)
        if profile == "iosxr":
            check = Check(
                "an Established eBGP session has the missing-policy marker (!), "
                "distinguishing policy rejection from a failed BGP transport",
                lambda o: bool(
                    re.search(r"^\d+\.\d+\.\d+\.\d+\s+.*\s\d+!\s*$", o, re.M)
                ),
            )
        elif profile == "routeros":
            check = contains(
                r"^undefined-bgp-policies=\d*[1-9]\d*\s*$",
                "an Established eBGP session names undefined input and output chains, "
                "distinguishing missing policy from session loss or defined rejection",
            )
        else:
            raise RuntimeCapabilityError(f"No native BGP policy evidence on {host}.")
        return OraclePlaybook(
            finding=f"{host}'s Established BGP sessions have no usable route policy.",
            steps=[
                router_cli(
                    self.net_env,
                    host,
                    f"Inspect {host}'s BGP policy bindings and session state.",
                    check,
                    frr=vtysh("show bgp summary"),
                    iosxr_command="show bgp ipv4 unicast summary",
                    routeros_command=(
                        ":local missing 0; "
                        ":foreach s in=[/routing bgp session find where established=yes] do={ "
                        ":local inChain [/routing bgp session get $s input.filter]; "
                        ":local outChain [/routing bgp session get $s output.filter-chain]; "
                        ":if (([/routing bgp session get $s remote.as] != "
                        "[/routing bgp session get $s local.as]) && "
                        "([:len $inChain] > 0) && ([:len $outChain] > 0) && "
                        "([/routing filter rule print count-only where chain=$inChain] = 0) && "
                        "([/routing filter rule print count-only where chain=$outChain] = 0)) "
                        "do={ :set missing ($missing + 1) } }; "
                        ':put ("undefined-bgp-policies=" . $missing)'
                    ),
                    evidence=True,
                    attempts=12,
                    interval_sec=10,
                ),
                router_cli(
                    self.net_env,
                    host,
                    f"Read {host}'s configured BGP neighbors and policies.",
                    frr=vtysh("show running-config"),
                    iosxr_command="show running-config router bgp",
                    routeros_command="/routing/bgp/connection/print terse",
                ),
            ],
        )

    def recover_fault(self, params: BGPSessionPolicyMissingParams) -> dict:
        from nika.problems.support.router_ops import native_fault_ops

        host = params.host_name
        ops = self._native(native_fault_ops(self, host, "restore_config"), host)
        saved = getattr(self, "_saved_config", None)
        peers = getattr(self, "_healthy_peers", None)
        routes = getattr(self, "_healthy_routes", None)
        if not saved or not peers or routes is None:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        ops.restore_config(saved)
        restored = wait_until(
            lambda: (
                peers <= ops.bgp_established_peers()
                and routes <= set(ops.remote_bgp_prefixes())
            ),
            180,
            interval_s=3,
        )
        dropped = ops.bgp_session_policy_dropped()
        return {
            "verified": restored and not dropped,
            "details": {"host": params.host_name, "policy_dropped": dropped},
        }


class BGPMd5PasswordMismatchParams(BaseModel):
    """Parameters for configuring TCP MD5 on one side of one BGP session."""

    host_name: str = Field(description="Target router host name.")
    neighbor_ip: str | None = Field(
        default=None,
        description="Neighbor address; defaults to the lowest established IPv4 peer.",
    )


class BGPMd5PasswordMismatch(ProblemBase):
    """A BGP neighbor requires TCP MD5 while its peer sends unsigned segments.

    RFC 2385 requires receivers to discard segments with a missing or invalid
    signature. Set a password on one router only and reset that session;
    the other router's authentication, TTL, policy and filters stay unchanged.
    Sources:
    https://www.rfc-editor.org/rfc/rfc2385
    https://docs.frrouting.org/en/latest/bgp.html#clicmd-neighbor-PEER-password-PASSWORD
    https://help.mikrotik.com/docs/spaces/ROS/pages/331612228/routing+bgp
    https://www.cisco.com/c/en/us/td/docs/ios_xr_sw/iosxr_r3-7/routing/command/reference/rr37bgp.html
    SR Linux neighbor authentication password:
    https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/protocol-authentication.html
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "bgp_md5_password_mismatch"
    description = (
        "A BGP neighbor requires a TCP MD5 password configured on one side only."
    )
    symptom_desc = "A routing session fails and withdraws the paths learned through it."
    effect_protocol = "bgp"
    TAGS = ["bgp"]
    COMPATIBLE_COLUMNS = frozenset(
        {
            "dc_clos",
            "enterprise_branch",
            "iosxr_simple_bgp",
            "routeros_simple_bgp",
            "isp_abilene/ebgp",
            "isp_abilene/ibgp_rr",
            "isp_dfn-bwin/ebgp",
            "isp_dfn-bwin/ibgp_rr",
        }
    )
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset(
        {
            "set_bgp_password",
            "bgp_password_configured",
            "bgp_established_peers",
            "reset_bgp_session",
            "ipv4_addresses",
            "bgp_received_prefixes",
        }
    )
    Params = BGPMd5PasswordMismatchParams
    RECORDED_ATTRS = ("neighbor_ip",)
    _PASSWORD = "r7Q4m2V9p6"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        plan = getattr(ctx.net_env, "bgp_plan", None)
        originators = sorted({item.device for item in getattr(plan, "originated", ())})
        return {"host_name": choice(ctx.rng, originators, ctx.router0)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "bgp"
        )
        plan = getattr(ctx.net_env, "bgp_plan", None)
        if plan is not None and inject.get("host_name") not in {
            item.device for item in plan.originated
        }:
            raise ValueError(
                f"{cls.root_cause_name} needs a router with a declared BGP prefix"
            )

    def root_cause_resources(self, params: BGPMd5PasswordMismatchParams):
        return [node_resource(params.host_name)]

    def _established(self, host: str) -> set[str]:
        ops = native_fault_ops(self, host, "bgp_established_peers")
        if ops is not None:
            return ops.bgp_established_peers()
        if self.lab_backend == "containerlab":
            return {
                peer
                for peer, row in self.runtime.lab_api.srl_bgp_neighbors(host).items()
                if row["state"] == "established"
            }
        data = json.loads(self.runtime.exec(host, "vtysh -c 'show bgp neighbors json'"))
        return {
            peer
            for peer, state in data.items()
            if isinstance(state, dict) and state.get("bgpState") == "Established"
        }

    def _check_tcp_md5(self, host: str) -> None:
        if (self.lab_backend == "containerlab") and native_router_profile(
            self.net_env, host
        ) is None:
            return
        if native_fault_ops(self, host, "set_bgp_password") is not None:
            return
        import shlex

        # Exercise setsockopt in the router's network namespace before mutation.
        script = (
            "import socket,struct; s=socket.socket(); "
            "addr=struct.pack('HH4s8s',socket.AF_INET,0,socket.inet_aton('127.0.0.2'),b'\\0'*8); "
            "s.setsockopt(socket.IPPROTO_TCP,14,addr+b'\\0'*112+"
            "struct.pack('BBHi',0,0,4,0)+b'q83z'+b'\\0'*76); print('TCP_MD5SIG_OK')"
        )
        output = self.runtime.exec(host, f"python3 -c {shlex.quote(script)}")
        if "TCP_MD5SIG_OK" not in output:
            raise RuntimeCapabilityError(
                f"TCP_MD5SIG is unavailable on {host!r}: {output}"
            )

    def _password_configured(self, host: str, peer: str) -> bool:
        ops = native_fault_ops(self, host, "bgp_password_configured")
        if ops is not None:
            return ops.bgp_password_configured(peer)
        if self.lab_backend == "containerlab":
            config = self.runtime.lab_api.srl_exec_cli(
                host, "info flat from running network-instance default protocols bgp"
            )
            return peer in _oracle_password_peers(config)
        config = self.runtime.exec(host, "vtysh -c 'show running-config'")
        return bool(
            re.search(rf"^ neighbor {re.escape(peer)} password .+$", config, re.M)
        )

    def _set_password(self, host: str, peer: str, password: str | None) -> None:
        ops = native_fault_ops(self, host, "set_bgp_password")
        if ops is not None:
            ops.set_bgp_password(peer, password)
        elif (self.lab_backend == "containerlab") and native_router_profile(
            self.net_env, host
        ) is None:
            path = f"/network-instance default protocols bgp neighbor {peer} authentication"
            self.runtime.lab_api.srl_configure(
                host,
                f"{path} password {password}"
                if password
                else f"delete {path} password",
            )
            self._reset(host, peer)
            return
        else:
            config = self.runtime.exec(host, "vtysh -c 'show running-config'")
            block = next(
                (
                    b
                    for b, peers in bgp_neighbors_by_instance(config).items()
                    if peer in peers and " vrf " not in b
                ),
                None,
            )
            if block is None:
                raise RuntimeCapabilityError(
                    f"No default-instance BGP neighbor {peer!r} on {host!r}."
                )
            command = (
                f"neighbor {peer} password {password}"
                if password is not None
                else f"no neighbor {peer} password"
            )
            output = self.runtime.exec(host, frr_config_command(block, command))
            if "%" in output or "Error" in output:
                raise RuntimeCapabilityError(
                    f"BGP password command failed on {host!r}: {output}"
                )
            frr_save_config(self.runtime, host)
        self._reset(host, peer)

    def _reset(self, host: str, peer: str) -> None:
        ops = native_fault_ops(self, host, "reset_bgp_session")
        if ops is not None:
            ops.reset_bgp_session(peer)
        elif (self.lab_backend == "containerlab") and native_router_profile(
            self.net_env, host
        ) is None:
            self.runtime.lab_api.srl_exec_cli(
                host,
                f"tools network-instance default protocols bgp neighbor {peer} reset-peer",
            )
            return
        else:
            self.runtime.exec(host, f"vtysh -c 'clear bgp {peer}'")

    def select_neighbor(self, params: BGPMd5PasswordMismatchParams) -> str:
        """Select an established IPv4 neighbor before baseline or injection."""
        peers = self._established(params.host_name)
        selected = params.neighbor_ip or getattr(self, "neighbor_ip", None)
        if not selected:
            addresses = sorted(
                ipaddress.IPv4Address(p)
                for p in peers
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p)
            )
            selected = str(addresses[0]) if addresses else None
        if selected is None or str(ipaddress.IPv4Address(selected)) not in peers:
            raise RuntimeCapabilityError(
                f"No established IPv4 BGP peer {selected!r} on {params.host_name!r}."
            )
        self.neighbor_ip = selected
        return selected

    def inject_fault(self, params: BGPMd5PasswordMismatchParams):
        host = params.host_name
        peer = self.select_neighbor(params)
        self._check_tcp_md5(host)
        if self._password_configured(host, peer):
            raise RuntimeCapabilityError(
                f"BGP neighbor {peer} on {host!r} already uses TCP MD5."
            )
        self._healthy_peers = self._established(host)
        self._set_password(host, peer, self._PASSWORD)

    def verify_fault(self, params: BGPMd5PasswordMismatchParams) -> dict:
        peer = getattr(self, "neighbor_ip", None)
        configured = bool(peer) and self._password_configured(params.host_name, peer)
        peers = self._established(params.host_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=configured and peer not in peers,
            details={
                "host": params.host_name,
                "neighbor_ip": peer,
                "password_configured": configured,
                "established_peers": sorted(peers),
            },
        )

    def oracle_playbook(self, params: BGPMd5PasswordMismatchParams) -> OraclePlaybook:
        host = params.host_name
        peers = _oracle_bgp_peers(self.net_env, host)
        if not peers:
            raise RuntimeCapabilityError(f"No BGP peer declared for {host}.")
        config = router_cli(
            self.net_env,
            host,
            f"Inspect TCP MD5 authentication on {host}'s BGP neighbors.",
            Check(
                "a BGP neighbor requires TCP MD5 authentication",
                lambda o: bool(_oracle_password_peers(o)),
            ),
            frr=vtysh("show running-config"),
            iosxr_command="show running-config router bgp",
            routeros_command=_ORACLE_ROS_PASSWORDS,
            srl_command="info flat from running network-instance default protocols bgp neighbor * authentication",
            evidence=True,
            save_as="md5_config",
        )

        def neighbor_args(outputs):
            # Select the neighbor from the observed authentication configuration.
            neighbor = _oracle_password_peers(outputs.get("md5_config", ""))[0]
            return router_cli(
                self.net_env,
                host,
                "Read the authenticated neighbor's session state.",
                frr=vtysh(f"show bgp neighbors {neighbor}"),
                iosxr_command=f"show bgp neighbor {neighbor}",
                routeros_command=(
                    "/routing/bgp/session/print terse; "
                    ":local up 0; "
                    ":foreach s in=[/routing bgp session find where established=yes] do={ "
                    # Session addresses can carry an @VRF suffix.
                    ":local addr [:tostr [/routing bgp session get $s remote.address]]; "
                    ':set addr [:pick $addr 0 [:find ($addr . "@") "@"]]; '
                    f':if ($addr="{neighbor}") do={{ :set up ($up + 1) }} }}; '
                    ':put ("authenticated-established=" . $up)'
                ),
                srl_command=f"info flat from state network-instance default protocols bgp neighbor {neighbor} session-state",
            ).args

        steps = [
            config,
            OracleStep(
                "Read the session state of the neighbor that requires TCP MD5.",
                config.tool,
                neighbor_args,
                contains(
                    r"BGP state = (?:Idle|Active|Connect|OpenSent|OpenConfirm)\b"
                    r"|^authenticated-established=0\s*$|session-state (?:idle|active|connect|opensent|openconfirm)\b",
                    "the authenticated session is not Established",
                ),
                evidence=True,
                attempts=18,
                interval_sec=10,
            ),
        ]

        def srl_unauthenticated(output):
            data = srl_json(output)
            if not data:
                return True
            bgp = data["network-instance"][0]["protocols"]["bgp"]
            return not any(
                n.get("authentication", {}).get("password")
                or n.get("authentication", {}).get("keychain")
                for n in bgp.get("neighbor", [])
            )

        for peer in peers:
            if (
                getattr(self.net_env, "backend", None) == "containerlab"
            ) and native_router_profile(self.net_env, peer) is None:
                steps.extend(
                    [
                        srl(
                            peer,
                            "info flat from running network-instance default protocols bgp neighbor * peer-group",
                            f"Read {peer}'s configured BGP neighbors.",
                            Check(
                                "the peer has configured BGP neighbors",
                                _oracle_bgp_config,
                            ),
                            evidence=True,
                        ),
                        srl(
                            peer,
                            "info from running network-instance default protocols bgp neighbor * authentication | as json",
                            f"Inspect authentication on {peer}'s BGP neighbors.",
                            Check(
                                "the configured peer has no BGP authentication password or keychain",
                                srl_unauthenticated,
                            ),
                            evidence=True,
                        ),
                    ]
                )
                continue
            steps.append(
                router_cli(
                    self.net_env,
                    peer,
                    f"Compare {peer}'s BGP authentication with {host}'s requirement.",
                    Check(
                        "the peer has BGP neighbors but no TCP MD5 password, "
                        "proving authentication is one-sided rather than an ACL or TTL fault",
                        lambda o: (
                            _oracle_bgp_config(o) and not _oracle_password_peers(o)
                        ),
                    ),
                    frr=vtysh("show running-config"),
                    iosxr_command="show running-config router bgp",
                    routeros_command=_ORACLE_ROS_PASSWORDS,
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=f"{host} requires TCP MD5 on a BGP session whose peer has no password.",
            steps=steps,
        )

    def recover_fault(self, params: BGPMd5PasswordMismatchParams) -> dict:
        peer = getattr(self, "neighbor_ip", None)
        healthy = getattr(self, "_healthy_peers", set())
        if peer and healthy:
            self._set_password(params.host_name, peer, None)
        restored = bool(healthy) and wait_until(
            lambda: healthy <= self._established(params.host_name), 180, interval_s=3
        )
        return {
            "verified": restored
            and not self._password_configured(params.host_name, peer),
            "details": {
                "host": params.host_name,
                "neighbor_ip": peer,
                "established_peers": sorted(self._established(params.host_name)),
            },
        }
