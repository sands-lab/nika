"""Frozen benchmark releases (``nika-bench@x.y.z``) with Dev/Test splits."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from nika.config import BENCHMARK_DIR, REPO_ROOT
from nika.net_env.kathara.utils.docker_files.docker_images import image_exists
from nika.net_env.net_env_pool import (
    get_net_env_instance,
    list_all_net_envs,
    scenario_requires_topo_size,
)
from nika.problems.prob_pool import get_problem_class, list_avail_problem_instances
from nika.service.mcp_server.registry import (
    MCP_SERVER_SPECS,
    select_diagnosis_servers,
    SUBMISSION_SERVER,
)
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.benchmark.resume import benchmark_row_fingerprint

BENCHMARK_ID = "nika-bench"
BENCHMARK_ID_ALIASES = frozenset({"nika-bench", "nika"})
DEFAULT_RELEASE_VERSION = "0.1.0"
RELEASES_DIR = BENCHMARK_DIR / "releases"
JOB_FILENAME = "benchmark_job.json"
RUN_CONFIG_FILENAME = "run.json"

SplitName = Literal["dev", "test"]
VALID_SPLITS: tuple[SplitName, ...] = ("dev", "test")

SCORING_V1 = {
    "id": "rule-based-v1",
    "description": (
        "detection accuracy; localization P/R/F1 on faulty_devices; "
        "RCA P/R/F1 on root_cause_name (nika.evaluator.scoring)"
    ),
    "leaderboard_primary": "rca_f1",
    "judge_allowed": False,
}

TOOLS_V1 = {
    "policy_id": "diagnosis-servers-v1",
    "allowed_mcp_servers": [
        "kathara_base_mcp_server",
        "pingmesh_mcp_server",
        "kathara_frr_mcp_server",
        "kathara_bmv2_mcp_server",
        "kathara_telemetry_mcp_server",
        "task_mcp_server",
    ],
}

RESOURCES_V1 = {
    "policy_id": "scenario-defined-v1",
}

DEFAULTS_V1 = {
    "case_timeout_sec": 2400,
    "n_trials": 3,
}


class ReleaseError(ValueError):
    """Invalid or unavailable benchmark release."""


@dataclass(frozen=True)
class BenchmarkRelease:
    """Resolved frozen release ready for preflight and execution."""

    id: str
    version: str
    root: Path
    split: SplitName
    cases_path: Path
    cases: list[dict[str, Any]]
    case_count: int
    cases_sha256: str
    splits: dict[str, Any]
    defaults: dict[str, Any]
    scoring: dict[str, Any]
    tools: dict[str, Any]
    resources: dict[str, Any]
    images: dict[str, Any]
    scenario_problem_pin: dict[str, Any]
    benchmark_digest: str
    manifest: dict[str, Any]

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def case_timeout_sec(self) -> int:
        return int(self.defaults.get("case_timeout_sec", 0))

    @property
    def n_trials(self) -> int:
        value = int(self.defaults.get("n_trials", 1))
        if value < 1:
            raise ReleaseError(f"Release defaults.n_trials must be >= 1 (got {value})")
        return value


def releases_dir() -> Path:
    return RELEASES_DIR


def list_releases() -> list[str]:
    """Return sorted release version directory names under ``benchmark/releases/``."""
    root = releases_dir()
    if not root.is_dir():
        return []
    versions: list[str] = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "RELEASE.yaml").is_file():
            versions.append(path.name)
    return versions


def normalize_split(split: str | None, *, default: SplitName) -> SplitName:
    if split is None or str(split).strip() == "":
        return default
    value = str(split).strip().lower()
    if value not in VALID_SPLITS:
        raise ReleaseError(f"Invalid split {split!r}; expected one of {VALID_SPLITS}")
    return value  # type: ignore[return-value]


def normalize_version_selector(selector: str) -> str:
    """Map short aliases like ``0.1`` → ``0.1.0`` when that release exists."""
    raw = selector.strip()
    if raw.startswith("sha256:"):
        return raw
    if raw in list_releases():
        return raw
    if raw.count(".") == 1:
        candidate = f"{raw}.0"
        if candidate in list_releases():
            return candidate
    return raw


def parse_release_ref(ref: str) -> tuple[str, str]:
    """Parse ``0.1.0``, ``nika@0.1``, ``nika-bench@0.1.0``, or ``…@sha256:<digest>``."""
    raw = (ref or "").strip()
    if not raw:
        raise ReleaseError("Empty release reference")
    if "@" not in raw:
        return BENCHMARK_ID, normalize_version_selector(raw)
    bench_id, selector = raw.split("@", 1)
    bench_id = bench_id.strip() or BENCHMARK_ID
    selector = selector.strip()
    if not selector:
        raise ReleaseError(f"Invalid release reference: {ref!r}")
    if bench_id not in BENCHMARK_ID_ALIASES:
        raise ReleaseError(
            f"Unknown benchmark id {bench_id!r}; expected one of "
            f"{sorted(BENCHMARK_ID_ALIASES)}"
        )
    return BENCHMARK_ID, normalize_version_selector(selector)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _repo_rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(resolved)


def _pin_source_file(path: Path) -> dict[str, str]:
    return {"path": _repo_rel(path), "sha256": _sha256_file(path)}


def _problem_source_path(problem_name: str) -> Path:
    cls = get_problem_class(problem_name)
    if cls is None:
        raise ReleaseError(f"Unknown problem: {problem_name!r}")
    return Path(inspect.getfile(cls)).resolve()


def _scenario_source_path(scenario_name: str) -> Path:
    envs = list_all_net_envs()
    if scenario_name not in envs:
        raise ReleaseError(f"Unknown scenario: {scenario_name!r}")
    return Path(inspect.getfile(envs[scenario_name])).resolve()


def collect_images_for_scenarios(scenario_names: set[str]) -> list[str]:
    images: set[str] = set()
    for name in sorted(scenario_names):
        kwargs: dict[str, Any] = {}
        if scenario_requires_topo_size(name):
            kwargs["topo_size"] = "s"
        env = get_net_env_instance(name, **kwargs)
        images.update(env._collect_lab_images())
    return sorted(images)


def build_scenario_problem_pins(
    scenarios: set[str], problems: set[str]
) -> dict[str, Any]:
    return {
        "scenarios": {
            name: _pin_source_file(_scenario_source_path(name))
            for name in sorted(scenarios)
        },
        "problems": {
            name: _pin_source_file(_problem_source_path(name))
            for name in sorted(problems)
        },
    }


def compute_benchmark_digest(
    *,
    splits: dict[str, Any],
    defaults: dict[str, Any],
    scoring: dict[str, Any],
    tools: dict[str, Any],
    resources: dict[str, Any],
    images: dict[str, Any],
    scenario_problem_pin: dict[str, Any],
) -> str:
    """Digest from policy + per-split ``cases_sha256`` (not full case bodies)."""
    split_payload = {
        name: {
            "case_count": int((meta or {}).get("case_count") or 0),
            "cases_sha256": str((meta or {}).get("cases_sha256") or ""),
        }
        for name, meta in sorted(splits.items())
    }
    payload = {
        "splits": split_payload,
        "defaults": defaults,
        "scoring": scoring,
        "tools": tools,
        "resources": resources,
        "images": {
            "required": sorted(images.get("required") or []),
        },
        "scenario_problem_pin": scenario_problem_pin,
    }
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def _load_manifest(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ReleaseError(f"Invalid RELEASE.yaml (expected mapping): {path}")
    return data


def _legacy_splits_from_manifest(
    root: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Support pre-split releases that only had ``cases_file`` / ``cases.yaml``."""
    cases_file = str(manifest.get("cases_file") or "cases.yaml")
    cases_path = root / cases_file
    if not cases_path.is_file() and (root / "dev.yaml").is_file():
        cases_file = "dev.yaml"
        cases_path = root / cases_file
    if not cases_path.is_file():
        raise ReleaseError(f"Missing cases file under {root}")
    return {
        "dev": {
            "cases_file": cases_file,
            "case_count": int(manifest.get("case_count") or 0),
            "cases_sha256": str(
                manifest.get("cases_sha256") or _sha256_file(cases_path)
            ),
        }
    }


def _resolve_splits(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("splits")
    if isinstance(raw, dict) and raw:
        return dict(raw)
    return _legacy_splits_from_manifest(root, manifest)


def _find_release_by_digest(digest: str) -> Path:
    needle = digest.removeprefix("sha256:").lower()
    for version in list_releases():
        release = load_release(version, split="dev", verify_digest=False)
        if release.benchmark_digest.lower() == needle:
            return release.root
    raise ReleaseError(f"No local release matches digest sha256:{needle}")


def resolve_release_dir(selector: str) -> Path:
    """Resolve a version or ``sha256:<digest>`` to a release directory."""
    if selector.startswith("sha256:"):
        return _find_release_by_digest(selector)
    root = releases_dir() / selector
    if not root.is_dir():
        raise ReleaseError(
            f"Release {selector!r} not found under {releases_dir()}. "
            f"Available: {', '.join(list_releases()) or '(none)'}"
        )
    return root


def load_release_from_dir(
    root: Path,
    *,
    split: SplitName = "dev",
    verify_digest: bool = True,
) -> BenchmarkRelease:
    """Load a release from an explicit directory containing ``RELEASE.yaml``."""
    root = Path(root)
    split = normalize_split(split, default="dev")
    manifest_path = root / "RELEASE.yaml"
    if not manifest_path.is_file():
        raise ReleaseError(f"Missing RELEASE.yaml in {root}")
    manifest = _load_manifest(manifest_path)
    splits = _resolve_splits(root, manifest)
    if split not in splits:
        raise ReleaseError(
            f"Release {root.name} has no {split!r} split; available: {sorted(splits)}"
        )

    split_meta = dict(splits[split] or {})
    cases_file = str(split_meta.get("cases_file") or f"{split}.yaml")
    cases_path = root / cases_file
    if not cases_path.is_file():
        raise ReleaseError(f"Missing {split} cases file: {cases_path}")

    cases = load_benchmark_yaml(cases_path)
    cases_sha256 = _sha256_file(cases_path)
    defaults = dict(manifest.get("defaults") or DEFAULTS_V1)
    scoring = dict(manifest.get("scoring") or SCORING_V1)
    tools = dict(manifest.get("tools") or TOOLS_V1)
    resources = dict(manifest.get("resources") or RESOURCES_V1)
    images = dict(manifest.get("images") or {"required": []})
    pins = dict(manifest.get("scenario_problem_pin") or {})

    # Refresh sha entries for digest from on-disk files when present.
    digest_splits: dict[str, Any] = {}
    for name, meta in splits.items():
        meta = dict(meta or {})
        path = root / str(meta.get("cases_file") or f"{name}.yaml")
        entry = {
            "cases_file": meta.get("cases_file") or f"{name}.yaml",
            "case_count": int(meta.get("case_count") or 0),
            "cases_sha256": str(meta.get("cases_sha256") or ""),
        }
        if path.is_file():
            entry["cases_sha256"] = _sha256_file(path)
            if not entry["case_count"]:
                entry["case_count"] = len(load_benchmark_yaml(path))
        digest_splits[name] = entry

    digest = compute_benchmark_digest(
        splits=digest_splits,
        defaults=defaults,
        scoring=scoring,
        tools=tools,
        resources=resources,
        images=images,
        scenario_problem_pin=pins,
    )
    stored = str(manifest.get("benchmark_digest") or "").removeprefix("sha256:")
    if verify_digest and stored and stored != digest:
        raise ReleaseError(
            f"Release digest mismatch for {root.name}: "
            f"RELEASE.yaml has {stored}, computed {digest}"
        )

    declared_count = int(split_meta.get("case_count") or 0)
    if declared_count and declared_count != len(cases):
        raise ReleaseError(
            f"{split} case_count {declared_count} does not match "
            f"{cases_path.name} ({len(cases)})"
        )

    stored_cases_sha = str(split_meta.get("cases_sha256") or "").removeprefix("sha256:")
    if stored_cases_sha and stored_cases_sha != cases_sha256:
        raise ReleaseError(
            f"{split} cases_sha256 mismatch: RELEASE.yaml has {stored_cases_sha}, "
            f"file hashes to {cases_sha256}"
        )

    version = str(manifest.get("version") or root.name)
    bench_id = str(manifest.get("id") or BENCHMARK_ID)
    return BenchmarkRelease(
        id=bench_id,
        version=version,
        root=root,
        split=split,
        cases_path=cases_path,
        cases=cases,
        case_count=len(cases),
        cases_sha256=cases_sha256,
        splits=digest_splits,
        defaults=defaults,
        scoring=scoring,
        tools=tools,
        resources=resources,
        images=images,
        scenario_problem_pin=pins,
        benchmark_digest=digest,
        manifest=manifest,
    )


def load_release(
    ref: str = DEFAULT_RELEASE_VERSION,
    *,
    split: SplitName | str = "dev",
    verify_digest: bool = True,
) -> BenchmarkRelease:
    """Load and resolve a frozen release by version or ``id@selector``."""
    _bench_id, selector = parse_release_ref(ref)
    root = resolve_release_dir(selector)
    return load_release_from_dir(
        root,
        split=normalize_split(split, default="dev"),
        verify_digest=verify_digest,
    )


def resolve_cases(
    ref: str = DEFAULT_RELEASE_VERSION,
    *,
    split: SplitName | str = "dev",
) -> list[dict[str, Any]]:
    return list(load_release(ref, split=split).cases)


def _verify_pins(pins: dict[str, Any], *, kind: str) -> None:
    entries = pins.get(kind) or {}
    if not isinstance(entries, dict):
        raise ReleaseError(f"scenario_problem_pin.{kind} must be a mapping")
    for name, meta in entries.items():
        if not isinstance(meta, dict) or "path" not in meta or "sha256" not in meta:
            raise ReleaseError(f"Invalid pin for {kind} {name!r}")
        path = Path(meta["path"])
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.is_file():
            raise ReleaseError(f"Pinned {kind} source missing for {name!r}: {path}")
        actual = _sha256_file(path)
        expected = str(meta["sha256"]).removeprefix("sha256:")
        if actual != expected:
            raise ReleaseError(
                f"Pinned {kind} {name!r} changed: expected sha256 {expected}, "
                f"got {actual} ({path})"
            )


def _verify_mcp_policy(cases: list[dict[str, Any]], tools: dict[str, Any]) -> None:
    allowed = set(tools.get("allowed_mcp_servers") or [])
    unknown = allowed - set(MCP_SERVER_SPECS)
    if unknown:
        raise ReleaseError(
            f"Release allowlist references unknown MCP servers: {sorted(unknown)}"
        )
    for row in cases:
        servers = set(select_diagnosis_servers(row["scenario"]))
        servers.add(SUBMISSION_SERVER)
        extra = servers - allowed
        if extra:
            raise ReleaseError(
                f"Scenario {row['scenario']!r} needs MCP servers outside allowlist: "
                f"{sorted(extra)}"
            )


def _fingerprints(cases: list[dict[str, Any]]) -> set[str]:
    return {benchmark_row_fingerprint(row) for row in cases}


def verify_dev_test_isolation(
    *,
    dev_cases: list[dict[str, Any]],
    test_cases: list[dict[str, Any]],
) -> None:
    """Ensure Dev/Test are disjoint instances covering the same failure types."""
    if not dev_cases or not test_cases:
        raise ReleaseError("Both Dev and Test splits must be non-empty")
    dev_problems = {row["problem"] for row in dev_cases}
    test_problems = {row["problem"] for row in test_cases}
    if dev_problems != test_problems:
        only_dev = sorted(dev_problems - test_problems)
        only_test = sorted(test_problems - dev_problems)
        raise ReleaseError(
            f"Dev/Test problem sets differ; only_dev={only_dev}, only_test={only_test}"
        )
    overlap = _fingerprints(dev_cases) & _fingerprints(test_cases)
    if overlap:
        raise ReleaseError(
            f"Dev/Test held-out isolation failed: {len(overlap)} shared fingerprint(s)"
        )


def preflight_release(
    release: BenchmarkRelease,
    *,
    check_images: bool = True,
) -> None:
    """Validate release integrity before any lab deploy.

    Raises ``ReleaseError`` on the first failure.
    """
    if release.case_count != len(release.cases):
        raise ReleaseError("Internal case_count mismatch")

    split_meta = release.splits.get(release.split) or {}
    declared = int(split_meta.get("case_count") or release.case_count)
    if declared != len(release.cases):
        raise ReleaseError(
            f"Manifest {release.split} case_count {declared} != "
            f"{release.cases_path.name} ({len(release.cases)})"
        )

    scenarios = {row["scenario"] for row in release.cases}
    problems = {row["problem"] for row in release.cases}
    known_scenarios = set(list_all_net_envs())
    known_problems = set(list_avail_problem_instances())
    missing_scenarios = sorted(scenarios - known_scenarios)
    missing_problems = sorted(problems - known_problems)
    if missing_scenarios:
        raise ReleaseError(f"Missing scenarios: {missing_scenarios}")
    if missing_problems:
        raise ReleaseError(f"Missing problems: {missing_problems}")

    # Isolation requires both splits on disk (always true for 0.1.0).
    if "dev" in release.splits and "test" in release.splits:
        dev = load_release_from_dir(
            release.root, split="dev", verify_digest=False
        ).cases
        test = load_release_from_dir(
            release.root, split="test", verify_digest=False
        ).cases
        verify_dev_test_isolation(dev_cases=dev, test_cases=test)

    _verify_pins(release.scenario_problem_pin, kind="scenarios")
    _verify_pins(release.scenario_problem_pin, kind="problems")
    _verify_mcp_policy(release.cases, release.tools)

    required = list(release.images.get("required") or [])
    if check_images:
        missing_images = [img for img in required if not image_exists(img)]
        if missing_images:
            raise ReleaseError(
                "Required Docker images are missing (release mode does not "
                f"auto-build): {missing_images}"
            )


def read_git_commit() -> tuple[str | None, bool]:
    """Return ``(commit_sha_or_None, dirty)`` for the repository root."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None, False
    try:
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        dirty = bool(dirty_out.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        dirty = False
    return commit or None, dirty


def build_job_metadata(
    release: BenchmarkRelease,
    *,
    agent_type: str | None,
    model: str | None,
    case_timeout_sec: int,
    official: bool,
    job_id: str | None = None,
    llm_provider: str | None = None,
    max_steps: int | None = None,
    n_trials: int = 1,
) -> dict[str, Any]:
    commit, dirty = read_git_commit()
    run_id = job_id or os.urandom(8).hex()
    return {
        "run_id": run_id,
        "job_id": run_id,
        "benchmark_id": release.id,
        "version": release.version,
        "benchmark_ref": release.ref,
        "benchmark_digest": release.benchmark_digest,
        "split": release.split,
        "case_count": release.case_count,
        "cases_sha256": release.cases_sha256,
        "nika_git_commit": commit,
        "nika_git_dirty": dirty,
        "scoring": release.scoring,
        "tools": {
            "policy_id": release.tools.get("policy_id"),
            "allowed_mcp_servers": release.tools.get("allowed_mcp_servers"),
        },
        "resources": release.resources,
        "defaults": release.defaults,
        "case_timeout_sec": case_timeout_sec,
        "agent_type": agent_type,
        "llm_provider": llm_provider,
        "model": model,
        "max_steps": max_steps,
        "n_trials": int(n_trials),
        "official": official,
    }


def load_run_config(result_dir: Path) -> dict[str, Any] | None:
    """Load run config from ``run.json`` or legacy ``benchmark_job.json``."""
    for name in (RUN_CONFIG_FILENAME, JOB_FILENAME):
        path = result_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return None


def write_job_metadata(result_dir: Path, job: dict[str, Any]) -> Path:
    """Write ``run.json`` plus legacy ``benchmark_job.json`` and lock file."""
    result_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(job, indent=2, sort_keys=True) + "\n"
    run_path = result_dir / RUN_CONFIG_FILENAME
    run_path.write_text(payload, encoding="utf-8")
    job_path = result_dir / JOB_FILENAME
    job_path.write_text(payload, encoding="utf-8")
    lock_path = result_dir / "RELEASE.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "benchmark_id": job.get("benchmark_id"),
                "version": job.get("version"),
                "benchmark_digest": job.get("benchmark_digest"),
                "split": job.get("split"),
                "nika_git_commit": job.get("nika_git_commit"),
                "nika_git_dirty": job.get("nika_git_dirty"),
                "agent_type": job.get("agent_type"),
                "model": job.get("model"),
                "n_trials": job.get("n_trials"),
                "official": job.get("official"),
                "run_id": job.get("run_id") or job.get("job_id"),
                "job_id": job.get("job_id") or job.get("run_id"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_path


def release_fields_for_session(job: dict[str, Any]) -> dict[str, Any]:
    """Subset of job metadata stamped onto each session ``run.json``."""
    run_id = job.get("run_id") or job.get("job_id")
    return {
        "benchmark_id": job.get("benchmark_id"),
        "benchmark_version": job.get("version"),
        "benchmark_digest": job.get("benchmark_digest"),
        "benchmark_split": job.get("split"),
        "benchmark_job_id": run_id,
        "benchmark_run_id": run_id,
        "benchmark_official": job.get("official"),
        "benchmark_n_trials": job.get("n_trials"),
        "scoring_id": (job.get("scoring") or {}).get("id"),
        "nika_git_commit": job.get("nika_git_commit"),
    }


def write_release_manifest(
    dest: Path,
    *,
    version: str,
    splits: dict[str, Any],
    defaults: dict[str, Any],
    scoring: dict[str, Any],
    tools: dict[str, Any],
    resources: dict[str, Any],
    images: dict[str, Any],
    scenario_problem_pin: dict[str, Any],
) -> str:
    digest = compute_benchmark_digest(
        splits=splits,
        defaults=defaults,
        scoring=scoring,
        tools=tools,
        resources=resources,
        images=images,
        scenario_problem_pin=scenario_problem_pin,
    )
    manifest = {
        "id": BENCHMARK_ID,
        "version": version,
        "splits": splits,
        "default_split_for_release": "test",
        "benchmark_digest": digest,
        "defaults": defaults,
        "scoring": scoring,
        "tools": tools,
        "resources": resources,
        "images": images,
        "scenario_problem_pin": scenario_problem_pin,
    }
    (dest / "RELEASE.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return digest


def freeze_release(
    *,
    version: str = DEFAULT_RELEASE_VERSION,
    source_cases: Path | None = None,
    out_dir: Path | None = None,
) -> BenchmarkRelease:
    """Create a minimal single-split (dev) release for tests / local experiments."""
    source = source_cases or (BENCHMARK_DIR / "benchmark_selected.yaml")
    dest = out_dir or (releases_dir() / version)
    dest.mkdir(parents=True, exist_ok=True)

    cases_raw = source.read_text(encoding="utf-8")
    cases_path = dest / "dev.yaml"
    cases_path.write_text(cases_raw, encoding="utf-8")
    cases = load_benchmark_yaml(cases_path)
    cases_sha256 = _sha256_file(cases_path)

    scenarios = {row["scenario"] for row in cases}
    problems = {row["problem"] for row in cases}
    pins = build_scenario_problem_pins(scenarios, problems)
    images = {"required": collect_images_for_scenarios(scenarios)}
    splits = {
        "dev": {
            "cases_file": "dev.yaml",
            "case_count": len(cases),
            "cases_sha256": cases_sha256,
        }
    }
    write_release_manifest(
        dest,
        version=version,
        splits=splits,
        defaults=dict(DEFAULTS_V1),
        scoring=dict(SCORING_V1),
        tools=dict(TOOLS_V1),
        resources=dict(RESOURCES_V1),
        images=images,
        scenario_problem_pin=pins,
    )
    return load_release_from_dir(dest, split="dev", verify_digest=True)
