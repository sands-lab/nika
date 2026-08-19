from nika.evaluator.result_log import resolve_failure_metadata
from nika.problems.prob_pool import get_problem_class


def test_failure_metadata_uses_one_domain_field() -> None:
    assert resolve_failure_metadata({"failure_domain": "link_interface"}) == {
        "failure_domain": "link_interface",
    }


def test_failure_metadata_merges_taxonomy_domain_from_problem() -> None:
    problem = get_problem_class("link_down")
    assert problem is not None
    resolved = resolve_failure_metadata({"problem_names": ["link_down"]})
    assert resolved["failure_domain"] == problem.taxonomy_metadata()["failure_domain"]


def test_failure_metadata_explicit_domain_wins_over_taxonomy() -> None:
    resolved = resolve_failure_metadata(
        {"failure_domain": "link_interface", "problem_names": ["link_down"]}
    )
    assert resolved["failure_domain"] == "link_interface"


def test_failure_metadata_missing_or_unknown_problem_yields_none() -> None:
    assert resolve_failure_metadata({"problem_names": []}) == {"failure_domain": None}
    assert resolve_failure_metadata({}) == {"failure_domain": None}
    assert resolve_failure_metadata({"problem_names": ["no_such_problem"]}) == {
        "failure_domain": None,
    }
