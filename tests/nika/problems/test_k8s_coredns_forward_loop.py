"""Corefile transformations preserve serving plugins and remove fallback upstreams."""

import pytest

from nika.problems.management_orchestration_plane.coredns import forward_to_self

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("forward", "expected"),
    [
        ("forward . /etc/resolv.conf", "forward . 127.0.0.1"),
        (
            "forward . 1.1.1.1 8.8.8.8 {\n        max_concurrent 1000\n    }",
            "forward . 127.0.0.1 {\n        max_concurrent 1000\n    }",
        ),
    ],
)
def test_loop_replaces_all_upstreams_and_preserves_plugins(forward, expected):
    template = ".:53 {\n    errors\n    kubernetes cluster.local {\n        pods insecure\n    }\n    %s\n    cache 30\n    loop\n    reload\n}\n"
    assert forward_to_self(template % forward) == template % expected


@pytest.mark.parametrize(
    "corefile",
    [
        ". {\n    forward . /etc/resolv.conf\n}\n",
        ". {\n    loop\n}\n",
        ". {\n    loop\n    forward . 1.1.1.1\n    forward . 8.8.8.8\n}\n",
    ],
)
def test_unsupported_corefile_is_rejected(corefile):
    with pytest.raises(ValueError):
        forward_to_self(corefile)
