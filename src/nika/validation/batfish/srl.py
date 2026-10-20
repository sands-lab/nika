"""Restricted IPv4 eBGP projection of concrete SR Linux update documents.

Batfish does not consume these documents natively. This maps addressed default
interfaces, direct eBGP neighbors and exact-prefix export policies to FRR input.
It is model evidence; SR Linux execution remains mandatory.
"""

from __future__ import annotations

import json
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path

import yaml

from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.snapshot import (
    prepare_snapshot,
    _cumulus_frr_snapshot,
    _edge,
)
from nika.net_env.synthesis.srl_updates import srl_config_document


def _document(value):
    if isinstance(value, str):
        value = yaml.safe_load(value)
    if isinstance(value, dict) and set(value) == {"operations"}:
        return srl_config_document(yaml.safe_dump(value, sort_keys=False))
    return value


def _is_netlab_default(document):
    (instance,) = document["srl_nokia-network-instance:network-instance"]
    return instance.get("type") == "default"


def _native_link_interface(endpoint):
    from nika.validation.batfish.srl_isp import native_interface

    prefix, index = endpoint.split("-", 1)
    if prefix != "e1" or not index.isdecimal() or int(index) < 1:
        raise ValueError(f"Unsupported SR Linux Containerlab interface {endpoint!r}")
    return native_interface(f"ethernet-1/{int(index)}.0")


def project_srl_bgp(node, document, *, shutdown=()):
    document = _document(document)
    if _is_netlab_default(document):
        from nika.validation.batfish.srl_isp import project_srl_isp

        return project_srl_isp(node, document, shutdown=shutdown, require_igp=False)
    if set(document) != {
        "srl_nokia-interfaces:interface",
        "srl_nokia-network-instance:network-instance",
        "srl_nokia-routing-policy:routing-policy",
    }:
        raise ValueError("Unsupported SR Linux document projection")
    (instance,) = document["srl_nokia-network-instance:network-instance"]
    if (
        instance["name"] != "default"
        or instance.get("admin-state") != "enable"
        or set(instance) != {"name", "admin-state", "interface", "protocols"}
        or set(instance["protocols"]) != {"srl_nokia-bgp:bgp"}
    ):
        raise ValueError(
            "SR Linux projection requires a default-instance BGP-only sketch"
        )
    bgp = instance["protocols"]["srl_nokia-bgp:bgp"]
    if set(bgp) - {
        "admin-state",
        "autonomous-system",
        "router-id",
        "afi-safi",
        "group",
        "neighbor",
    }:
        raise ValueError("Unsupported SR Linux BGP attributes")
    if bgp.get("admin-state") != "enable" or bgp["afi-safi"] != [
        {"afi-safi-name": "ipv4-unicast", "admin-state": "enable"}
    ]:
        raise ValueError("SR Linux projection requires enabled IPv4-unicast BGP")
    if any(
        set(g) - {"group-name", "admin-state"} or g.get("admin-state") != "enable"
        for g in bgp["group"]
    ):
        raise ValueError("Inherited BGP group attributes are outside this projection")
    attached = {i["name"] for i in instance["interface"]}
    addresses = {}
    lines = [f"hostname {node}"]
    for index, interface in enumerate(document["srl_nokia-interfaces:interface"]):
        if set(interface) - {"name", "admin-state", "subinterface"}:
            raise ValueError("Unsupported SR Linux interface attributes")
        (sub,) = interface["subinterface"]
        name = f"{interface['name']}.{sub['index']}"
        if (
            name not in attached
            or set(sub) - {"index", "admin-state", "ipv4", "ipv6"}
            or set(sub["ipv4"]) - {"admin-state", "address"}
        ):
            raise ValueError("Unsupported SR Linux subinterface projection")
        address = sub["ipv4"]["address"]
        if len(address) != 1:
            raise ValueError("Projection requires one IPv4 address per subinterface")
        address = IPv4Interface(address[0]["ip-prefix"])
        addresses[str(address.network)] = str(address.ip)
        # Batfish sees stable synthetic interface names only within this model.
        lines += [f"interface eth{index}", f" ip address {address}"]
        if (
            (node, interface["name"].replace("ethernet-", "e").replace("/", "-"))
            in shutdown
            or interface.get("admin-state") != "enable"
            or sub.get("admin-state", "enable") != "enable"
            or sub["ipv4"].get("admin-state", "enable") != "enable"
        ):
            lines += [" shutdown"]
        lines += ["!"]
    routing = document["srl_nokia-routing-policy:routing-policy"]
    prefix_sets = {}
    for pset in routing["prefix-set"]:
        prefixes = []
        for item in pset["prefix"]:
            prefix = IPv4Network(item["ip-prefix"])
            if (
                set(item) != {"ip-prefix", "mask-length-range"}
                or item["mask-length-range"]
                != f"{prefix.prefixlen}..{prefix.prefixlen}"
            ):
                raise ValueError("Only exact prefix matches are modeled")
            prefixes.append(str(prefix))
        prefix_sets[pset["name"]] = prefixes
    policies = {p["name"]: p for p in routing["policy"]}
    # Local origin prefix-set belongs to the fixed sketch, not solver output.
    origins = prefix_sets.get("NC-ORIGIN", [])
    if not set(origins) <= set(addresses):
        raise ValueError("SR Linux BGP origins must be connected prefixes")
    lines += [
        f"router bgp {bgp['autonomous-system']}",
        f" bgp router-id {bgp['router-id']}",
        " no bgp ebgp-requires-policy",
    ]
    for prefix in origins:
        lines += [f" network {prefix}"]
    exports = []
    for index, neighbor in enumerate(bgp["neighbor"]):
        if set(neighbor) - {
            "peer-address",
            "peer-as",
            "peer-group",
            "admin-state",
            "import-policy",
            "export-policy",
        }:
            raise ValueError("Unsupported SR Linux neighbor attributes")
        if (
            neighbor.get("admin-state") != "enable"
            or neighbor.get("import-policy") != ["NC-ACCEPT"]
            or policies["NC-ACCEPT"]
            != {"name": "NC-ACCEPT", "default-action": {"policy-result": "accept"}}
        ):
            raise ValueError("Projection requires explicit accept-all imports")
        (policy_name,) = neighbor["export-policy"]
        policy = policies[policy_name]
        if set(policy) != {"name", "statement"}:
            raise ValueError("Unsupported export policy")
        peer = neighbor["peer-address"]
        lines += [
            f" neighbor {peer} remote-as {neighbor['peer-as']}",
            f" neighbor {peer} route-map NC-{index} out",
        ]
        statements = policy["statement"]
        if statements[-1] != {"name": "65535", "action": {"policy-result": "reject"}}:
            raise ValueError("Export policies must end in explicit reject")
        for seq, statement in enumerate(statements[:-1], 1):
            if (
                set(statement) != {"name", "match", "action"}
                or set(statement["match"]) != {"prefix-set"}
                or set(statement["action"]) != {"policy-result"}
            ):
                raise ValueError("Only exact-prefix permit/deny policies are modeled")
            result = statement["action"]["policy-result"]
            if result not in {"accept", "reject"}:
                raise ValueError("Unsupported policy action")
            name = f"NC-{index}-{seq}"
            for pseq, prefix in enumerate(
                prefix_sets[statement["match"]["prefix-set"]], 1
            ):
                exports.append(f"ip prefix-list {name} seq {pseq * 5} permit {prefix}")
            exports += [
                f"route-map NC-{index} {'permit' if result == 'accept' else 'deny'} {seq * 10}",
                f" match ip address prefix-list {name}",
                "!",
            ]
    return "\n".join([*lines, "!", *exports]) + "\n"


def build_srl_snapshot(env, contract, root: Path, *, configs=None, shutdown=()):
    documents = (
        env.routing_documents
        if configs is None
        else {n: _document(c) for n, c in configs.items()}
    )
    documents = {node: _document(document) for node, document in documents.items()}
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    native_nodes = {
        node: _is_netlab_default(document) for node, document in documents.items()
    }
    interface_names = {}
    aliases = {}
    if not all(native_nodes.values()):
        interface_names = {
            node: {
                i["name"].replace("ethernet-", "e").replace("/", "-"): f"eth{index}"
                for index, i in enumerate(doc["srl_nokia-interfaces:interface"])
            }
            for node, doc in documents.items()
            if not native_nodes[node]
        }
    if any(native_nodes.values()):
        from nika.validation.batfish.srl_isp import (
            canonical_interface,
            native_interface,
            srl_addresses,
        )

        aliases = {
            node: {
                native_interface(name): canonical_interface(name)
                for _, name in srl_addresses(document)
            }
            for node, document in documents.items()
            if native_nodes[node]
        }
    for node, document in documents.items():
        content = project_srl_bgp(node, document, shutdown=shutdown)
        if native_nodes[node]:
            (root / "configs" / f"{node}.cfg").write_text(content)
        else:
            (root / "configs" / f"{node}.cfg").write_text(
                _cumulus_frr_snapshot(
                    node, "frr version 10.5\nfrr defaults traditional\n" + content
                )
            )
    edges = []
    for link in env.get_lab_spec().links:
        a, b = (endpoint.split(":") for endpoint in link.endpoints)
        if a[0] in documents and b[0] in documents:
            if native_nodes[a[0]] and native_nodes[b[0]]:
                edges.append(
                    _edge(
                        a[0],
                        _native_link_interface(a[1]),
                        b[0],
                        _native_link_interface(b[1]),
                    )
                )
            elif not native_nodes[a[0]] and not native_nodes[b[0]]:
                edges.append(
                    _edge(
                        a[0],
                        interface_names[a[0]][a[1]],
                        b[0],
                        interface_names[b[0]][b[1]],
                    )
                )
            else:
                raise ValueError("Cannot mix native and FRR SR Linux projection nodes")
    (root / "batfish" / "layer1_topology.json").write_text(
        json.dumps({"edges": edges}) + "\n"
    )
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routing_model": "srl-bgp-projection",
            "native_config_analysis": False,
            "interface_aliases": aliases,
            "projection_evidence": "concrete transformed SR Linux configuration",
            "scope": "Concrete IPv4 default-instance routing configuration projected to Batfish; native SR Linux execution remains authoritative",
        },
    )
