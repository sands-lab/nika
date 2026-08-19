import importlib
import inspect
import pkgutil
from typing import Dict, Type

from nika.problems.multi_problems import MultiFaultProblem
from nika.problems.problem_base import ProblemBase
from nika.utils.logger import system_logger

logger = system_logger

# Legacy failure ids rewritten at load/inject time (no separate registry entry).
_PROBLEM_ALIASES: dict[str, str] = {
    "host_vpn_membership_missing": "wireguard_peer_key_misconfiguration",
    "link_fragmentation_disabled": "mtu_mismatch",
}


def resolve_problem_name(problem_name: str) -> str:
    """Map a legacy failure id to the registered ``root_cause_name``."""
    return _PROBLEM_ALIASES.get(problem_name, problem_name)


def _register_problems() -> dict[str, type[ProblemBase]]:
    """Register single-fault problem classes keyed by root_cause_name."""
    problems: dict[str, type[ProblemBase]] = {}
    package = importlib.import_module("nika.problems")

    for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
        if info.name.count(".") == package.__name__.count(".") + 1:
            continue

        try:
            module = importlib.import_module(info.name)
        except Exception as e:
            logger.warning(f"Failed to import {info.name}: {e}")
            continue

        try:
            members = inspect.getmembers(module, inspect.isclass)
        except Exception as e:
            logger.warning(f"Failed to inspect members of {info.name}: {e}")
            continue

        for cls_name, cls_obj in members:
            if cls_obj.__module__ != module.__name__:
                continue
            if not (
                inspect.isclass(cls_obj)
                and issubclass(cls_obj, ProblemBase)
                and cls_obj is not ProblemBase
            ):
                continue

            try:
                meta = cls_obj.META
                if meta is None:
                    continue
                root_cause_name = meta.root_cause_name
                if not root_cause_name:
                    continue
                problems[root_cause_name] = cls_obj
            except Exception as e:
                logger.warning(
                    f"Failed to register class {cls_name} in {info.name}: {e}"
                )
                continue
    return problems


_PROBLEMS: Dict[str, Type[ProblemBase]] = _register_problems()


def list_avail_problem_names() -> list[str]:
    """List all available root cause names."""
    return list(_PROBLEMS.keys())


def list_avail_problem_instances() -> dict[str, type[ProblemBase]]:
    return _PROBLEMS


def list_avail_tags() -> list[str]:
    """List all available tags for problems."""
    tags: set[str] = set()
    for problem_class in _PROBLEMS.values():
        tags.update(problem_class.TAGS)
    return list(tags)


def get_problem_class(problem_name: str) -> type[ProblemBase] | None:
    """Return the registered class for *problem_name*, or None.

    Legacy aliases (e.g. ``host_vpn_membership_missing``,
    ``link_fragmentation_disabled``) resolve to the current failure id.
    """
    return _PROBLEMS.get(resolve_problem_name(problem_name))


def get_problem_instance(
    problem_names: list, scenario_name: str, **kwargs
) -> ProblemBase:
    """Get a problem instance for the given root cause name(s)."""
    if not isinstance(problem_names, list) or len(problem_names) == 0:
        raise ValueError("problem_names should be a list of problem_names.")

    resolved = [resolve_problem_name(name) for name in problem_names]

    if len(resolved) > 1:
        sub_faults = [
            _PROBLEMS[fault_name](scenario_name=scenario_name, **kwargs)
            for fault_name in resolved
        ]
        return MultiFaultProblem(
            sub_faults=sub_faults, scenario_name=scenario_name, **kwargs
        )

    return _PROBLEMS[resolved[0]](scenario_name=scenario_name, **kwargs)
