"""Low-limit diagnosis covers an established peer and a later prefix overflow."""

import pytest

from nika.problems.routing_control_plane.bgp_prefix_surge import _headroom_evidence

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("state", "accepted", "limit", "reason", "expected"),
    [
        ("Established", 3, 4, "", True),
        ("Established", 3, 40, "", False),
        ("Idle", 0, 4, "Peer had exceeded the max. no. of prefixes configured", True),
        ("Idle", 0, 40, "Peer had exceeded the max. no. of prefixes configured", False),
        ("Idle", 0, 4, "Hold Timer Expired", False),
    ],
)
def test_headroom_evidence_distinguishes_a_low_limit_from_other_resets(
    state, accepted, limit, reason, expected
):
    output = (
        f"BGP state = {state}\n  {accepted} accepted, 3 sent prefixes\n"
        f"Maximum prefixes allowed {limit}\nLast reset: {reason}\n"
    )
    assert _headroom_evidence(output, normal_count=3) is expected


def test_low_limit_oracle_excludes_suppressed_rpki_content_prefixes():
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.routing_control_plane.bgp_prefix_surge import BGPLowPrefixLimit

    env = load_offline_net_env(
        "isp_abilene",
        igp="ospf",
        bgp_mode="ebgp",
        backend="kathara",
        device_profile="frr",
    )
    site = env.bgp_plan.inventory["customer_site"]
    problem = BGPLowPrefixLimit.__new__(BGPLowPrefixLimit)
    problem.net_env = env
    plan = problem.oracle_playbook(
        problem.Params(
            receiver_name=site["partner"],
            peer_name=site["provider"],
            neighbor_ip=site["provider_ip"],
        )
    )
    # Live customer-peer evidence: three normal prefixes, a limit of four,
    # and a later surge that holds the session down.
    overflow = (
        "BGP state = Idle\n0 accepted prefixes\nMaximum prefixes allowed 4\n"
        "Peer had exceeded the max. no. of prefixes configured.\n"
    )
    assert plan.steps[0].expect(overflow)
    assert not plan.steps[0].expect(overflow.replace("allowed 4", "allowed 40"))
