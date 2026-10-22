"""Assemble the supported subset of SR Linux path/value operations as a tree."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_LIST_KEYS = {
    "network-instance": {"name"},
    "dns-instance": {"name"},
    "host-entry": {"name"},
    "interface": {"name", "interface-name"},
    "subinterface": {"index"},
    "address": {"ip-prefix"},
    "instance": {"name"},
    "area": {"area-id"},
    "neighbor": {"peer-address"},
    "group": {"group-name"},
    "afi-safi": {"afi-safi-name"},
    "level": {"level-number"},
    "statement": {"name"},
    "prefix-set": {"name"},
    "prefix": {"ip-prefix"},
    "policy": {"name"},
    "community-set": {"name"},
}
_INTEGER_KEYS = {"index", "level-number", "instance-id"}
_PATH_FIELDS = {
    "afi-safi",
    "bgp",
    "community-set",
    "export-policy",
    "group",
    "import-policy",
    "interface",
    "instance",
    "isis",
    "neighbor",
    "network-instance",
    "ospf",
    "policy",
    "prefix-set",
    "protocols",
    "routing-policy",
    "subinterface",
}


def _segments(path: str) -> list[tuple[str, tuple[str, Any] | None]]:
    if not path.startswith("/"):
        raise ValueError(f"SRL operation path must be absolute: {path!r}")
    parts, start, depth = [], 1, 0
    for pos, char in enumerate(path[1:], 1):
        depth += (char == "[") - (char == "]")
        if depth < 0:
            raise ValueError(f"Malformed SRL operation path: {path!r}")
        if char == "/" and depth == 0:
            parts.append(path[start:pos])
            start = pos + 1
    if depth:
        raise ValueError(f"Malformed SRL operation path: {path!r}")
    parts.append(path[start:])
    result = []
    for part in parts:
        match = re.fullmatch(r"([^\[\]]+)(?:\[([^=\]]+)=([^\]]+)\])?", part)
        if not match:
            raise ValueError(f"Unsupported SRL path segment: {part!r}")
        field, key, raw = match.groups()
        if field not in _PATH_FIELDS:
            raise ValueError(f"Unsupported SRL path field {field!r}")
        value = int(raw) if key in _INTEGER_KEYS else raw
        result.append((field, (key, value) if key is not None else None))
    return result


def _identity(field: str, row: Mapping[str, Any]) -> str:
    candidates = _LIST_KEYS.get(field, set()) & row.keys()
    if len(candidates) != 1:
        raise ValueError(
            f"Cannot infer unique identity for {field!r} row: {dict(row)!r}"
        )
    return next(iter(candidates))


def _merge_rows(
    dst: list[dict[str, Any]], rows: Sequence[Mapping[str, Any]], field: str
) -> None:
    for row in rows:
        key = _identity(field, row)
        matches = [item for item in dst if item.get(key) == row[key]]
        if len(matches) > 1:
            raise ValueError(f"Duplicate SRL {field} identity {key}={row[key]!r}")
        if matches:
            _merge(matches[0], row)
        else:
            target: dict[str, Any] = {}
            _merge(target, row)
            dst.append(target)


def _merge(dst: dict[str, Any], src: Mapping[str, Any]) -> None:
    for field, incoming in src.items():
        if field.startswith("_annotate_"):
            continue
        current = dst.get(field)
        if isinstance(incoming, Mapping):
            if field in _LIST_KEYS:
                _identity(field, incoming)
                rows = current if isinstance(current, list) else []
                if current is not None and not isinstance(current, list):
                    raise ValueError(f"Conflicting SRL list value at {field!r}")
                dst[field] = rows
                _merge_rows(rows, [incoming], field)
            else:
                if current is None:
                    current = {}
                    dst[field] = current
                if not isinstance(current, dict):
                    raise ValueError(f"Conflicting SRL object value at {field!r}")
                _merge(current, incoming)
        elif (
            isinstance(incoming, list) and incoming and isinstance(incoming[0], Mapping)
        ):
            rows = current if isinstance(current, list) else []
            if current is not None and not isinstance(current, list):
                raise ValueError(f"Conflicting SRL list value at {field!r}")
            dst[field] = rows
            _merge_rows(rows, incoming, field)
        else:
            dst[field] = incoming


def _path_target(
    root: dict[str, Any], segments: Sequence[tuple[str, tuple[str, Any] | None]]
) -> tuple[dict[str, Any], str | None]:
    target = root
    for field, selector in segments[:-1]:
        if selector is None:
            child = target.setdefault(field, {})
            if not isinstance(child, dict):
                raise ValueError(f"Path conflicts with existing SRL value at {field!r}")
            target = child
            continue
        key, value = selector
        if key not in _LIST_KEYS.get(field, set()):
            raise ValueError(f"Unexpected key {key!r} for SRL list {field!r}")
        rows = target.setdefault(field, [])
        if not isinstance(rows, list):
            raise ValueError(f"Path conflicts with existing SRL list at {field!r}")
        matches = [row for row in rows if row.get(key) == value]
        if len(matches) > 1:
            raise ValueError(f"Duplicate SRL list identity {key}={value!r}")
        if matches:
            target = matches[0]
        else:
            target = {key: value}
            rows.append(target)
    field, selector = segments[-1]
    if selector is None:
        return target, field
    key, value = selector
    if key not in _LIST_KEYS.get(field, set()):
        raise ValueError(f"Unexpected key {key!r} for SRL list {field!r}")
    rows = target.setdefault(field, [])
    if not isinstance(rows, list):
        raise ValueError(f"Path conflicts with existing SRL list at {field!r}")
    matches = [row for row in rows if row.get(key) == value]
    if len(matches) > 1:
        raise ValueError(f"Duplicate SRL list identity {key}={value!r}")
    if matches:
        return matches[0], None
    selected = {key: value}
    rows.append(selected)
    return selected, None


def assemble_srl_updates(operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge supported update operations; leaf replace is intentionally narrow."""
    root: dict[str, Any] = {}
    for operation in operations:
        if set(operation) not in ({"path", "value"}, {"operation", "path", "value"}):
            raise ValueError(
                "SRL operation must contain path/value and optional operation"
            )
        action = operation.get("operation", "update")
        segments = _segments(operation["path"])
        target, leaf = _path_target(root, segments)
        value = operation["value"]
        if action == "replace":
            if leaf is None or isinstance(value, Mapping):
                raise ValueError(
                    "SRL replace supports only terminal, unselected scalar/list leaves"
                )
            target[leaf] = value
        elif action == "update":
            if not isinstance(value, Mapping):
                raise ValueError("SRL update value must be a mapping")
            if leaf is None:
                _merge(target, value)
            else:
                child = target.setdefault(leaf, {})
                if not isinstance(child, dict):
                    raise ValueError(f"SRL update path conflicts at {leaf!r}")
                _merge(child, value)
        else:
            raise ValueError(f"Unsupported SRL operation {action!r}")
    return root


def srl_config_document(text: str) -> dict:
    """Read upstream operations or replay a saved JSON-IETF configuration tree."""
    import yaml

    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError("SR Linux configuration must be a mapping")
    if set(document) == {"operations"}:
        document = assemble_srl_updates(document["operations"])
        names = {
            "interface": "srl_nokia-interfaces:interface",
            "network-instance": "srl_nokia-network-instance:network-instance",
            "routing-policy": "srl_nokia-routing-policy:routing-policy",
        }
        if set(document) - names.keys():
            raise ValueError("Unsupported SR Linux configuration root")
        document = {names[k]: v for k, v in document.items()}
        for instance in document["srl_nokia-network-instance:network-instance"]:
            instance["protocols"] = {
                f"srl_nokia-{k}:{k}": v
                for k, v in instance.get("protocols", {}).items()
            }
    if (
        not {
            "srl_nokia-interfaces:interface",
            "srl_nokia-network-instance:network-instance",
        }
        <= document.keys()
    ):
        raise ValueError("Incomplete SR Linux configuration tree")
    return document
