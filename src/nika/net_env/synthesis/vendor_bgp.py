"""Render exact-prefix BGP export parameters on native vendor configurations."""

from __future__ import annotations

import re
from ipaddress import IPv4Network

from nika.net_env.synthesis.parameters import RoutingParameters


def _solved_exports(node: str, parameters, neighbors: set[str]):
    solved = RoutingParameters.model_validate(parameters)
    if any(values for kind, values in solved if kind != "bgp_exports"):
        raise ValueError("Native vendor completion supports BGP exports only")
    policies = [policy for policy in solved.bgp_exports if policy.node == node]
    solved_neighbors = [policy.neighbor for policy in policies]
    if len(solved_neighbors) != len(set(solved_neighbors)):
        raise ValueError(f"Duplicate solved BGP neighbor on {node}")
    if set(solved_neighbors) != neighbors:
        raise ValueError(f"Solved BGP exports must cover every neighbor on {node}")
    return sorted(policies, key=lambda policy: policy.neighbor)


def render_iosxr_bgp_exports(config: str, node: str, parameters) -> str:
    """Replace known PASS outbound attachments with exact-prefix XR policies."""
    neighbor_pattern = re.compile(
        r"^\s*neighbor ([0-9.]+)\s*\n"
        r"(?:(?!^\s*neighbor |^\s*!\s*$).)*?"
        r"^\s*route-policy (\S+) out\s*$",
        re.MULTILINE | re.DOTALL,
    )
    attachment_list = []
    for match in neighbor_pattern.finditer(config):
        neighbor, policy = match.groups()
        attachment_list.append((neighbor, policy))
    if len({neighbor for neighbor, _policy in attachment_list}) != len(attachment_list):
        raise ValueError(f"Duplicate IOS-XR BGP neighbor on {node}")
    attachments = dict(attachment_list)
    if not attachments:
        raise ValueError(f"No native IOS-XR BGP neighbors found on {node}")
    if any(policy != "PASS" for policy in attachments.values()):
        raise ValueError("Cannot replace an unknown IOS-XR export policy")

    policies = _solved_exports(node, parameters, set(attachments))
    rendered = config
    additions = []
    for policy in policies:
        neighbor = policy.neighbor
        name = f"NIKA-OUT-{node}-{neighbor.replace('.', '-')}"
        statements = []
        terms = sorted(policy.terms, key=lambda item: item.prefix)
        for index, term in enumerate(terms, 1):
            prefix_set = f"{name}-P{index}"
            prefix = IPv4Network(term.prefix)
            statements.extend(
                [
                    f"prefix-set {prefix_set}",
                    f" {prefix}",
                    " end-set",
                    "!",
                ]
            )
        statements.append(f"route-policy {name}")
        for index, term in enumerate(terms, 1):
            prefix_set = f"{name}-P{index}"
            statements.extend(
                [
                    f" if destination in {prefix_set} then",
                    "  done" if term.permit else "  drop",
                    " endif",
                ]
            )
        statements.extend([" drop", "end-policy", "!"])
        additions.extend(statements)
        attach = re.compile(
            rf"(^\s*neighbor {re.escape(neighbor)}\s*\n"
            rf"(?:(?!^\s*neighbor |^\s*!\s*$).)*?"
            rf"^\s*)route-policy PASS out$",
            re.MULTILINE | re.DOTALL,
        )
        rendered, count = attach.subn(rf"\1route-policy {name} out", rendered, count=1)
        if count != 1:
            raise ValueError(f"Could not bind IOS-XR export policy for {neighbor}")
    block = "!\n" + "\n".join(additions) + "\n"
    if not re.search(r"^router bgp\s+", rendered, re.MULTILINE):
        raise ValueError("Expected a default-VRF IOS-XR BGP instance")
    return re.sub(
        r"^router bgp\s+", block + "router bgp ", rendered, count=1, flags=re.MULTILINE
    )


def render_routeros_bgp_exports(config: str, node: str, parameters) -> str:
    """Bind exact-prefix RouterOS output chains to existing BGP connections."""
    connection_pattern = re.compile(
        r"^(/routing bgp connection add .*?)$", re.MULTILINE
    )
    connections = list(connection_pattern.finditer(config))
    by_neighbor: dict[str, re.Match] = {}
    for match in connections:
        line = match.group(1)
        remote = re.search(r"(?:^|\s)remote\.address=([0-9.]+)(?:\s|$)", line)
        if remote:
            by_neighbor.setdefault(remote.group(1), match)
            if (
                remote.group(1) in by_neighbor
                and by_neighbor[remote.group(1)] is not match
            ):
                raise ValueError(
                    f"Duplicate RouterOS BGP neighbor on {node}: {remote.group(1)}"
                )
    if not by_neighbor:
        raise ValueError(f"No native RouterOS BGP neighbors found on {node}")

    policies = _solved_exports(node, parameters, set(by_neighbor))
    rendered = config
    additions = []
    for policy in policies:
        match = by_neighbor[policy.neighbor]
        line = match.group(1)
        if re.search(r"(?:^|\s)output\.filter-chain=", line):
            raise ValueError("Cannot replace an unknown RouterOS export filter")
        name = f"NIKA-OUT-{node}-{policy.neighbor.replace('.', '-')}"
        replacement = line + f" output.filter-chain={name}"
        rendered = rendered.replace(line, replacement, 1)
        for term in sorted(policy.terms, key=lambda item: item.prefix):
            prefix = IPv4Network(term.prefix)
            action = "accept" if term.permit else "reject"
            additions.append(
                f"/routing filter rule add chain={name} "
                f'rule="if (dst == {prefix} && dst-len == {prefix.prefixlen}) '
                f'{{ {action}; }}"'
            )
        additions.append(f'/routing filter rule add chain={name} rule="reject;"')
    return "\n".join(additions) + "\n" + rendered.lstrip()
