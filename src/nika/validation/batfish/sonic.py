"""Export SONiC routers for Batfish's native SONiC parser.

Batfish reads one ``sonic_configs/<router>/`` folder per SONiC router, with the
``config_db.json`` that holds ports and addresses and the ``frr.conf`` that
holds routing. NIKA writes the exact files it deploys.

The pinned Batfish FRR model has no IS-IS. A design whose only IGP is IS-IS is
therefore analyzed through the restricted IOS XR projection that Kathara FRR
IS-IS uses, applied to the same FRR design under its original interface names.
When IS-IS runs next to OSPF, the native model analyzes OSPF and BGP and the
snapshot reports IS-IS as unmodeled, so IS-IS intents stay unsupported.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from ipaddress import ip_interface
from pathlib import Path

from nika.net_env.utils.sonic.common import sonic_port
from nika.net_env.utils.sonic.render import SONIC_PREAMBLE, SonicRouter, config_db
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.snapshot import (
    SNAPSHOT_METADATA_FILENAME,
    _edge,
    prepare_snapshot,
)

ROUTING_MODEL = "sonic"
_INTERFACE = re.compile(r"interface (\S+)")
_RENAMED = re.compile(
    r"(\s*neighbor \S+ update-source |\s*(?:no )?passive-interface )(\S+)"
)


def design_frr(router: SonicRouter, frr_conf: str | None = None) -> str:
    """Return the FRR design a SONiC router renders, under its design names.

    This inverts :func:`translate_frr_design`: SONiC interface names go back
    to the design's names, CONFIG_DB addresses go back into the interface
    blocks, and the SONiC logging and zebra preamble is dropped.
    """
    names = router.design_names
    lines = []
    seen = set()
    for line in (router.frr_conf if frr_conf is None else frr_conf).splitlines():
        if line.strip() in SONIC_PREAMBLE:
            continue
        if not line.startswith(" ") and (match := _INTERFACE.fullmatch(line)):
            seen.add(match[1])
            lines.append(f"interface {names.get(match[1], match[1])}")
            lines.extend(_address_lines(router, match[1]))
            continue
        if match := _RENAMED.fullmatch(line):
            line = match[1] + names.get(match[2], match[2])
        lines.append(line)
    for iface in router.addresses:
        if iface not in seen:
            lines += [f"interface {names.get(iface, iface)}"]
            lines += [*_address_lines(router, iface), "!"]
    return "\n".join(lines) + "\n"


def design_addresses(router: SonicRouter) -> list[tuple[str, str]]:
    """IPv4 ``(address/len, design interface)`` pairs from CONFIG_DB."""
    return [
        (address, router.design_names.get(iface, iface))
        for iface, addresses in router.addresses.items()
        for address in addresses
        if ip_interface(address).version == 4
    ]


def _address_lines(router: SonicRouter, iface: str) -> list[str]:
    return [
        f" {'ip' if ip_interface(a).version == 4 else 'ipv6'} address {a}"
        for a in router.addresses.get(iface, ())
    ]


def _port(iface: str) -> str:
    """SONiC port of a Containerlab endpoint (``ethN``) or SONiC port name."""
    return iface if iface.startswith("Ethernet") else sonic_port(iface)


def _igps(routers: dict[str, SonicRouter]) -> set[str]:
    return {
        igp
        for router in routers.values()
        for igp in ("ospf", "isis")
        if re.search(rf"^router {igp}\b", router.frr_conf, re.MULTILINE)
    }


def build_sonic_snapshot(
    env,
    contract,
    root: str | Path,
    *,
    configs: dict[str, str] | None = None,
    shutdown=(),
    routing_model: str = ROUTING_MODEL,
) -> ValidationSnapshot:
    """Build a Batfish snapshot from an environment's SONiC routers.

    ``configs`` replaces routers' ``frr.conf``; CONFIG_DB stays as rendered.
    ``shutdown`` lists ``(router, interface)`` pairs to disable, named by
    Containerlab endpoint (``ethN``) or SONiC port.
    """
    if routing_model != ROUTING_MODEL:
        raise ValueError("Unsupported SONiC routing projection")
    routers = dict(env.sonic_routers)
    if configs is not None:
        if set(configs) != set(routers):
            raise ValueError(
                "SONiC candidate routers do not match the deployment: "
                f"missing={sorted(set(routers) - set(configs))}, "
                f"unknown={sorted(set(configs) - set(routers))}"
            )
        routers = {
            name: replace(router, frr_conf=configs[name])
            for name, router in routers.items()
        }
    disabled = {(node, _port(iface)) for node, iface in shutdown if node in routers}
    igps = _igps(routers)
    projected = igps == {"isis"}
    snapshot_root = prepare_snapshot(root)
    (snapshot_root / "batfish").mkdir()

    if projected:
        router_iface, aliases = _write_isis_projection(
            env, snapshot_root, routers, disabled
        )
    else:
        aliases = {name: dict(r.design_names) for name, r in routers.items()}
        for name, router in sorted(routers.items()):
            db = config_db(router)
            for port in (p for n, p in disabled if n == name):
                db["PORT"][port]["admin_status"] = "down"
            folder = snapshot_root / "sonic_configs" / name
            folder.mkdir(parents=True)
            _write_json(folder / "config_db.json", db)
            (folder / "frr.conf").write_text(router.frr_conf, encoding="utf-8")

        def router_iface(node: str, port: str) -> str:
            return port

    hosts = sorted(
        getattr(getattr(env, "traffic", None), "hosts", ()), key=lambda h: h.host_name
    )
    if hosts:
        (snapshot_root / "hosts").mkdir()
    for host in hosts:
        _write_json(
            snapshot_root / "hosts" / f"{host.host_name}.json",
            {
                "hostname": host.host_name,
                "hostInterfaces": {
                    host.host_iface: {
                        "name": host.host_iface,
                        "prefix": f"{host.address}/{host.prefixlen}",
                        "gateway": host.gateway,
                    }
                },
            },
        )
    modeled = set(routers) | {host.host_name for host in hosts}
    edges = []
    for link in env.get_lab_spec().links:
        ends = [endpoint.split(":", 1) for endpoint in link.endpoints]
        if not all(node in modeled for node, _ in ends):
            continue
        edges.append(
            _edge(
                *(
                    item
                    for node, iface in ends
                    for item in (
                        node,
                        router_iface(node, _port(iface)) if node in routers else iface,
                    )
                )
            )
        )
    _write_json(snapshot_root / "batfish" / "layer1_topology.json", {"edges": edges})

    metadata = {
        "contract_id": contract.contract_id,
        "routing_model": routing_model,
        "routers": sorted(routers),
        "router_count": len(routers),
        "host_count": len(hosts),
        "layer1_edge_count": len(edges),
        "native_config_analysis": not projected,
        "interface_aliases": aliases,
        "unmodeled_protocols": sorted(igps & {"isis"}) if not projected else [],
        "scope": (
            "SONiC FRR IPv4 IS-IS L2-only wide metrics and optional ordinary iBGP RR "
            "or direct eBGP projected to IOS XR; CONFIG_DB interface addresses"
            if projected
            else "SONiC CONFIG_DB ports and IPv4 addresses with FRR OSPFv2, BGP, "
            "prefix lists and route maps parsed natively; IPv6 and IS-IS unmodeled"
        ),
    }
    Path(root).mkdir(parents=True, exist_ok=True)
    _write_json(Path(root) / SNAPSHOT_METADATA_FILENAME, metadata)
    return ValidationSnapshot(
        snapshot_id=contract.contract_id, path=snapshot_root, metadata=metadata
    )


def _write_isis_projection(env, snapshot_root: Path, routers, disabled):
    """Write the IOS XR IS-IS projection.

    Returns the SONiC-port-to-IOS-XR name mapping and the IOS XR interface
    aliases back to design names.
    """
    from nika.validation.batfish.isis import (
        _native_interface,
        project_frr_isis,
    )

    bgp_plan = getattr(env, "bgp_plan", None)
    stubs = bgp_plan.stub_interfaces() if bgp_plan is not None else {}
    (snapshot_root / "configs").mkdir()
    for name, router in sorted(routers.items()):
        down = [(name, router.design_names[port]) for n, port in disabled if n == name]
        (snapshot_root / "configs" / f"{name}.cfg").write_text(
            project_frr_isis(
                name,
                design_frr(router),
                shutdown=down,
                allow_ebgp=bgp_plan is not None,
                stub_addresses=stubs.get(name, ()),
            ),
            encoding="utf-8",
        )

    def router_iface(node: str, port: str) -> str:
        return _native_interface(routers[node].design_names[port])

    aliases = {
        name: {
            _native_interface(design): design
            for design in router.design_names.values()
            if re.fullmatch(r"lo|eth[0-9]+", design)
        }
        for name, router in routers.items()
    }
    return router_iface, aliases


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
