"""Map every issue a run wrote to the reference issues it describes, with one LLM.

    uv run python -m experiment.submission_audit.map_issues \\
        --runs runs/ --base-url http://host:port/v1 --model Qwen3.8-27B-FP8 \\
        --out-name mapping_llm.yaml

The same mapper scores every run and every kind of issue (text signatures and
cross-trial statistics alike), so recall over all reference issues uses one
rule. The model sees the reference issues (id, class, title, and the summary
of the skill issue that records it, if any) and one written issue (title,
class, summary, signatures, scope, evidence), and returns the ids of the
reference issues that describe the same defect.

``--validate`` compares the result with an existing mapping (for example the
hand-checked mapping of the agent runs) and reports agreement.
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yaml

from experiment.submission_audit import skill  # noqa: F401  (adds auditlib to sys.path)

from experiment.submission_audit.scan_eval import CLI

from auditlib.kb import KB_DIR
from auditlib.scan import ScanConfig, chat

HERE = Path(__file__).resolve().parent

SYSTEM = """\
You match audit issues to a list of known reference issues for NIKA, a \
benchmark for network troubleshooting agents. An audit issue describes a \
defect of the benchmark, its harness and tools, or a submission's run.

A written issue matches a reference issue when both describe the same defect: \
the same thing is wrong, in the same environment, tool, fault type, or harness \
component. Wording and level of detail may differ, as long as a maintainer \
reading the written issue would be led to that reference issue. Rules:
- The written issue must name what the reference issue names: the specific \
artifact, file, message, tool, environment, fault type, or component. A \
general detector (any leak term, any truncated output, any harness path) does \
not match a specific reference issue.
- Matching only the class or the general topic is not enough.
- When in doubt, do not match. Most written issues match one reference issue \
or none.

Reference issues:
{references}

Reply with JSON only: {{"refs": ["<reference id>", ...], "reason": "<one sentence>"}}"""

_JSON = re.compile(r"\{.*\}", re.S)


def references_text() -> str:
    refs = yaml.safe_load((HERE / "reference_issues.yaml").read_text())
    issue_refs = yaml.safe_load((HERE / "issue_refs.yaml").read_text())
    summary: dict[str, str] = {}
    for issue_id, ids in issue_refs.items():
        path = KB_DIR / "issues" / f"{issue_id}.yaml"
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text())
        for ref in ids:
            summary.setdefault(
                ref, " ".join(str(data.get("summary", "")).split())[:300]
            )
    lines = []
    for r in refs:
        line = f"- {r['id']} [{r['class']}] {r['title']}"
        if summary.get(r["id"]):
            line += f" — {summary[r['id']]}"
        lines.append(line)
    return "\n".join(lines)


def issue_text(data: dict) -> str:
    keep = {
        k: data.get(k)
        for k in ("id", "class", "title", "summary", "scope", "detect", "evidence")
        if data.get(k)
    }
    prov = data.get("provenance") or {}
    keep["sources"] = (prov.get("refs") or [])[:5]
    return yaml.safe_dump(keep, sort_keys=False, allow_unicode=True, width=100)[:6000]


def map_run(run: Path, cfg: ScanConfig, system: str, valid: set[str]) -> dict:
    """Map the issues the run learned; the memory it started with is skipped."""
    steps = sorted(
        p for p in run.iterdir() if p.is_dir() and p.name[:3].rstrip("_").isdigit()
    )
    initial = (
        {p.stem for p in (steps[0] / "kb_before" / "issues").glob("*.yaml")}
        if steps
        else set()
    )
    files = sorted(
        p for p in (run / "kb" / "issues").glob("*.yaml") if p.stem not in initial
    )
    texts = []
    for path in files:
        try:
            texts.append(issue_text(yaml.safe_load(path.read_text()) or {}))
        except yaml.YAMLError:
            texts.append(path.read_text()[:6000])

    def one(text: str) -> list[str]:
        result = chat(client, cfg, system, text)
        match = _JSON.search(result.get("content") or "")
        try:
            data = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            data = {}
        return sorted({r for r in data.get("refs") or [] if r in valid})

    with httpx.Client(limits=httpx.Limits(max_connections=cfg.workers)) as client:
        with ThreadPoolExecutor(cfg.workers) as pool:
            refs = list(pool.map(one, texts))
    return {p.stem: r for p, r in zip(files, refs)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--out-name", default="mapping_llm.yaml")
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--mode", choices=("on", "off"), default="on")
    parser.add_argument(
        "--validate", help="Existing mapping file name to compare with."
    )
    parser.add_argument(
        "--cli",
        choices=tuple(CLI),
        help="Use an agent CLI instead of the HTTP endpoint.",
    )
    args = parser.parse_args()
    valid = {
        r["id"] for r in yaml.safe_load((HERE / "reference_issues.yaml").read_text())
    }
    system = SYSTEM.format(references=references_text())
    cfg = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        workers=args.workers,
        max_tokens=16384 if args.mode == "on" else 2048,
        thinking=args.mode == "on",
        command=CLI.get(args.cli),
        timeout=600,
    )
    runs = sorted(p for p in args.runs.iterdir() if (p / "kb" / "issues").is_dir())
    for run in runs:
        mapping = map_run(run, cfg, system, valid)
        (run / args.out_name).write_text(
            "# Written issues mapped to reference issues by the LLM mapper.\n"
            + yaml.safe_dump(mapping, width=100)
        )
        line = f"{run.name}: {len(mapping)} issues, {sum(bool(v) for v in mapping.values())} mapped"
        if args.validate and (run / args.validate).is_file():
            other = yaml.safe_load((run / args.validate).read_text()) or {}
            pairs = {(i, r) for i, rs in mapping.items() for r in rs}
            gold = {(i, r) for i, rs in other.items() if i in mapping for r in rs or []}
            both = len(pairs & gold)
            line += (
                f"; vs {args.validate}: precision {both / max(1, len(pairs)):.2f}, "
                f"recall {both / max(1, len(gold)):.2f}"
            )
        print(line, flush=True)


if __name__ == "__main__":
    main()
