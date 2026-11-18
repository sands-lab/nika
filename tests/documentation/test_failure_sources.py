"""The public source reference covers the registry and stays reproducible."""

import pytest

from nika.problems.registry import list_avail_problem_instances
from scripts.failure_sources import (
    ROOT,
    load_catalog,
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
