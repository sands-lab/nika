"""Traffic-control units and per-class evidence used by overlay QoS faults."""

import json

import pytest

from nika.problems.traffic_queueing_resource.overlay_qos import linux_queue_state

pytestmark = pytest.mark.unit


def test_tc_queue_rates_are_bytes_per_second_and_counters_are_per_class():
    output = json.dumps(
        [
            {
                "handle": "1:20",
                "rate": 500000,
                "ceil": 500000,
                "stats": {"bytes": 9000, "packets": 7, "drops": 0},
            },
            {
                "handle": "1:10",
                "rate": 50000,
                "ceil": 50000,
                "stats": {"bytes": 2464, "packets": 2, "drops": 18},
            },
        ]
    )
    assert linux_queue_state(output, "1:10") == {
        "rate_kbps": 400,
        "ceil_kbps": 400,
        "bytes": 2464,
        "packets": 2,
        "drops": 18,
    }


@pytest.mark.parametrize("profile", ["frr", "routeros"])
def test_ef_oracle_requires_fresh_drops_with_undersized_class_and_ef_marking(profile):
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.problems.registry import get_problem_class

    cls = get_problem_class("overlay_qos_ef_undersized")
    problem = cls.__new__(cls)
    problem.net_env = get_net_env_instance(
        "enterprise_branch", topo_size="s", device_profile=profile
    )
    params = cls.Params(
        host_name="hq_edge",
        intf_name="wg_br1",
        src_host="hq_corp_pc",
        dst_host="br1_corp_pc",
    )
    evidence = problem.oracle_playbook(params).steps[-1]
    rate = problem.qos(params).ef_kbit // 10

    def output(drops, *, undersized=True, classified=True):
        bandwidth = rate if undersized else rate * 10
        if profile == "routeros":
            from nika.net_env.enterprise_branch.topology import routeros_rate

            return (
                f"0 name=wg_br1-ef limit-at={routeros_rate(bandwidth)} "
                f"max-limit={routeros_rate(bandwidth)}\n"
                + ("dscp=46 new-packet-mark=wg_br1-ef\n" if classified else "")
                + f"packets=4419 dropped={drops}"
            )
        return (
            json.dumps(
                [
                    {
                        "handle": "1:10",
                        "rate": bandwidth * 1000 // 8,
                        "ceil": bandwidth * 1000 // 8,
                        "stats": {"bytes": 5479560, "packets": 4419, "drops": drops},
                    }
                ]
            )
            + ("\nmatch 00b80000/00fc0000 flowid 1:10" if classified else "")
            + f"\nSent 5479560 bytes 4419 pkt (dropped {drops}, overlimits 0)"
        )

    saved = {"ef_before": output(1063)}
    assert evidence.attempts > 1
    assert not evidence.expect(output(1063), saved)
    assert evidence.expect(output(1100), saved)
    assert not evidence.expect(output(1100, undersized=False), saved)
    assert not evidence.expect(output(1100, classified=False), saved)
