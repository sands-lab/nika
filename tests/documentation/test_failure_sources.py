"""The public source reference covers the registry and stays reproducible."""

import pytest

from nika.problems.combinations import list_combinations
from nika.problems.registry import list_avail_problem_instances
from scripts.failure_sources import (
    ROOT,
    load_catalog,
    load_combination_catalog,
    render_combinations,
    render_markdown,
    validate_catalog,
)

pytestmark = pytest.mark.contract


def test_failure_sources_cover_the_registry_and_render_the_reference():
    catalog = load_catalog()
    registry = list_avail_problem_instances()
    validate_catalog(catalog, registry)
    assert catalog.keys() == registry.keys()
    reference = (ROOT / "docs/operations/failures.md").read_text()
    assert reference == render_markdown(catalog, registry, reference)


def test_every_combination_cites_its_relation_and_renders_the_reference():
    combinations = {item.name: item for item in list_combinations()}
    catalog = load_combination_catalog()
    assert catalog.keys() == combinations.keys()
    assert all(catalog.values())
    reference = (ROOT / "docs/operations/failures.md").read_text()
    assert reference == render_combinations(catalog, combinations, reference)
