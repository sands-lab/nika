"""enterprise_branch business traffic keeps its QoS and NAT sizing contracts."""

from __future__ import annotations

import pytest

from nika.config import pkg_path
from nika.net_env.enterprise_branch.topology import (
    OVERLAY_QOS,
    build_topo_spec,
    overlay_qos_for,
    overlay_qos_scale,
)
from nika.net_env.enterprise_branch.workload import (
    GUEST_ARGS,
    REGIONAL_LOAD,
    RegionalLoad,
    VIDEO_PAYLOAD,
    build_workload,
)
from nika.traffic.workload import NAT_SESSIONS, REALTIME_BULK_CONTENTION

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("rate_limit_kbps", [None, 1000])
@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_regional_egress_is_unsaturated_until_ef_is_demoted(
    size: str, rate_limit_kbps: int | None
) -> None:
    qos = overlay_qos_for(size, rate_limit_kbps)
    scale = overlay_qos_scale(OVERLAY_QOS[size], rate_limit_kbps)
    full = REGIONAL_LOAD[size]
    load = RegionalLoad(
        full.sync_pcs,
        int(full.connection_kbps * scale),
        int(full.video_kbps * scale),
    )
    workload = build_workload(build_topo_spec(size), [], rate_limit_kbps)
    rates = {a for p in workload.processes if p.command == "filedist" for a in p.args}
    assert f"{load.connection_kbps}k" in rates
    if rate_limit_kbps:
        assert qos.rate_kbit <= rate_limit_kbps
    be_kbps = qos.rate_kbit - qos.ef_kbit
    # Headroom for a slow host servicing the shaper.
    assert load.sync_kbps <= 0.65 * be_kbps
    # Healthy media fits its EF guarantee, RTP/UDP/IP headers included.
    assert load.video_kbps * (VIDEO_PAYLOAD + 40) / (VIDEO_PAYLOAD + 12) <= (
        qos.ef_kbit
    )
    assert load.sync_kbps + load.video_kbps > 1.1 * be_kbps
    # Demoted, the video gets at most its fair share of BE or what the sync
    # leaves, so it loses a large part of its packets.
    left = max(be_kbps - load.sync_kbps, be_kbps / (load.sync_pcs + 1))
    assert 1 - left / load.video_kbps > 0.25
    # EF may borrow up to the link rate while BE stays below its ceiling.
    assert load.sync_kbps + load.video_kbps < qos.rate_kbit


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_workload_programs_exist_and_cover_declared_targets(size: str) -> None:
    spec = build_topo_spec(size)
    workload = build_workload(spec, ["hq_edge"])
    nodes = set(spec.all_node_names())
    assert {p.node for p in workload.processes} <= nodes
    for process in workload.processes:
        assert pkg_path("traffic", "apps", f"{process.app}.py").is_file()
    assert workload.covers(REALTIME_BULK_CONTENTION, ("hq_edge", "wg_br1"))
    assert ("br1_edge", "10.1.40.0/24") in workload.targets(NAT_SESSIONS)
    # FRR routers passed in also run the routine change agent.
    assert any(
        p.node == "hq_edge" and p.app == "change_window" for p in workload.processes
    )


def test_guest_peak_concurrency_exceeds_a_64_port_pool() -> None:
    args = dict(zip(GUEST_ARGS[::2], GUEST_ARGS[1::2]))
    assert int(args["--sessions"]) + int(args["--burst"]) > 64
