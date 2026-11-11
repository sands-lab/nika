"""Validate public fault evidence and report coverage against the live registry."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs/operations/failure-sources.json"
SOURCE_TYPES = (
    "incident_report",
    "operator_mailing_list",
    "vendor_documentation",
    "rfc",
    "paper",
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: Literal[
        "incident_report",
        "operator_mailing_list",
        "vendor_documentation",
        "rfc",
        "paper",
    ]
    title: str = Field(min_length=1)
    url: HttpUrl
    locator: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    supports: Literal["mechanism", "reference_implementation", "symptom"]
    correspondence: str = Field(min_length=1)
    limitation: str = Field(min_length=1)
    accessed: date


class FaultEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    implementation: str
    symbol: str
    sources: list[Evidence] = Field(min_length=1)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_catalog(path: Path = CATALOG) -> dict[str, FaultEvidence]:
    raw = json.loads(path.read_text(), object_pairs_hook=unique_object)
    return {name: FaultEvidence.model_validate(record) for name, record in raw.items()}


def validate_catalog(catalog, registry):
    unknown = catalog.keys() - registry.keys()
    if unknown:
        raise ValueError(f"Unregistered failure IDs: {sorted(unknown)}")
    for name, record in catalog.items():
        cls = registry[name]
        expected_path = Path(__import__(cls.__module__, fromlist=["__file__"]).__file__)
        if (ROOT / record.implementation).resolve() != expected_path.resolve():
            raise ValueError(f"Implementation path differs from registry: {name}")
        if record.symbol != cls.__name__:
            raise ValueError(f"Implementation symbol differs from registry: {name}")
        urls = [str(source.url) for source in record.sources]
        if len(set(urls)) != len(urls):
            raise ValueError(f"Duplicate source URL for {name}")


def report(catalog, registry):
    counts = Counter(
        source.source_type for item in catalog.values() for source in item.sources
    )
    fault_counts = Counter(
        kind
        for item in catalog.values()
        for kind in {s.source_type for s in item.sources}
    )
    return {
        "registered_faults": len(registry),
        "reviewed_faults": len(catalog),
        "pending_faults": sorted(registry.keys() - catalog.keys()),
        "source_records_by_type": {kind: counts[kind] for kind in SOURCE_TYPES},
        "faults_by_source_type": {kind: fault_counts[kind] for kind in SOURCE_TYPES},
        "faults": {
            name: item.model_dump(mode="json") for name, item in sorted(catalog.items())
        },
    }


def render_csv(catalog, registry):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Failure ID", "Domain", "Sources", "Source types"])
    for name, cls in sorted(registry.items()):
        sources = catalog[name].sources if name in catalog else []
        writer.writerow(
            [
                name,
                str(cls.failure_domain),
                " ; ".join(str(s.url) for s in sources),
                " ; ".join(sorted({s.source_type for s in sources})),
            ]
        )
    return output.getvalue()


def render_markdown(catalog, registry, reference):
    lines = []
    in_table = False
    has_sources = False
    seen = set()
    for line in reference.splitlines():
        if line.startswith("| Root cause | Failure ID |"):
            in_table = True
            has_sources = line.endswith(" Sources |")
            lines.append(line if has_sources else line + " Sources |")
            continue
        if in_table and not line.startswith("|"):
            in_table = False
        if in_table and line.startswith("| ---"):
            lines.append(line if has_sources else line + " --- |")
            continue
        match = re.match(r"^\| [^|]+\| `([^`]+)` \|", line) if in_table else None
        if match and match[1] in registry:
            name = match[1]
            seen.add(name)
            if has_sources:
                line = line.rsplit("|", 2)[0] + "|"
            sources = catalog[name].sources if name in catalog else []
            links = (
                "; ".join(
                    f"[{source.title}](#source-{name}-{index + 1})"
                    for index, source in enumerate(sources)
                )
                or "Pending"
            )
            line += f" {links} |"
        lines.append(line)
    if seen != registry.keys():
        raise ValueError(
            f"Failure reference rows differ from registry: {sorted(seen ^ registry.keys())}"
        )
    reference = "\n".join(lines) + "\n"
    result = report(catalog, registry)
    evidence = [
        "<!-- failure-sources:start -->",
        f"Reviewed: {result['reviewed_faults']} of {result['registered_faults']} registered failures.",
        "",
        "| Source type | Citation records | Distinct failures |",
        "| --- | --- | --- |",
    ]
    for kind in SOURCE_TYPES:
        evidence.append(
            f"| {kind} | {result['source_records_by_type'][kind]} | {result['faults_by_source_type'][kind]} |"
        )
    evidence.extend(
        [
            "",
            "Citation records count repeated uses of a source. Distinct-failure counts overlap when a failure cites multiple source types.",
            "",
        ]
    )
    for name, fault in sorted(catalog.items()):
        evidence.extend(
            [
                f"### `{name}`",
                "",
                f"Implementation: [`{fault.symbol}`](../../{fault.implementation}).",
                "",
            ]
        )
        for index, source in enumerate(fault.sources, 1):
            evidence.extend(
                [
                    f'<a id="source-{name}-{index}"></a>',
                    "<details>",
                    f"<summary>{html.escape(source.title)} ({source.source_type}; {source.supports})</summary>",
                    "",
                    f"Source: [{source.title}]({source.url}). Location: {source.locator}. Accessed: {source.accessed}.",
                    "",
                    f"> {html.escape(source.excerpt)}",
                    "",
                    f"**Correspondence:** {source.correspondence}",
                    "",
                    f"**Evidence limits:** {source.limitation}",
                    "",
                    "</details>",
                    "",
                ]
            )
    evidence.append("<!-- failure-sources:end -->")
    block = "\n".join(evidence)
    start, end = "<!-- failure-sources:start -->", "<!-- failure-sources:end -->"
    if reference.count(start) != 1 or reference.count(end) != 1:
        raise ValueError("Expected one public-source evidence block in failures.md")
    before, rest = reference.split(start)
    _, after = rest.split(end)
    return before + block + after


def main():
    from nika.problems.registry import list_avail_problem_instances

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--format", choices=("json", "csv", "markdown"), default="json")
    parser.add_argument(
        "--write-docs",
        action="store_true",
        help="Update source columns and evidence in failures.md.",
    )
    args = parser.parse_args()
    catalog = load_catalog()
    registry = list_avail_problem_instances()
    validate_catalog(catalog, registry)
    result = report(catalog, registry)
    if args.write_docs:
        reference = ROOT / "docs/operations/failures.md"
        reference.write_text(render_markdown(catalog, registry, reference.read_text()))
        return int(args.require_complete and bool(result["pending_faults"]))
    if args.format == "csv":
        print(render_csv(catalog, registry), end="")
    elif args.format == "markdown":
        print(
            render_markdown(
                catalog, registry, (ROOT / "docs/operations/failures.md").read_text()
            ),
            end="",
        )
    else:
        print(json.dumps(result, indent=2))
    return int(args.require_complete and bool(result["pending_faults"]))


if __name__ == "__main__":
    raise SystemExit(main())
