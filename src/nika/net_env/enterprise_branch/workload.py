"""Business traffic of the enterprise_branch WAN (always on, every case).

- **File sync.** HQ SERVER runs a file distribution service with a
  per-connection rate limit. Branch br1 is the regional office: every CORP PC
  syncs from it back to back, sized so br1's HQ overlay egress carries steady
  best-effort load below its BE ceiling. Other branches carry no bulk
  transfers, which keeps the shared hubs and underlay lightly loaded.
- **Calls.** HQ CORP holds a video link with br1 and voice calls with every
  other branch and DC2, all RTP marked DSCP EF. The br1 video nearly fills the
  EF reservation of that egress. Every softphone logs per-call receive
  quality (packets, loss, jitter) like a real endpoint's call statistics.
- **Internet.** One GUEST host per site stands for the guest Wi-Fi: persistent
  long-poll sessions, page loads of parallel short connections, and photo
  uploads to the public web service behind ``isp1``, through the Site Edge's
  source NAT. One IOT host per site (``m``/``l``) keeps telemetry sessions to
  the same service.

The load on br1's HQ overlay egress stays well below the BE ceiling, so
healthy BE traffic does not queue; EF media that lost its marking would push
that class past its ceiling and lose packets to its fair queueing.
"""

from __future__ import annotations

from dataclasses import dataclass

from nika.net_env.enterprise_branch.addressing import (
    edge_name_for,
    isp_name_for,
    lan_host_ip,
)
from nika.net_env.enterprise_branch.topology import (
    PUBLIC_SERVICE_IP,
    SYNC_OBJECT,
    LanSpec,
    TopoSize,
    TopoSpec,
    hub_iface_for,
    overlay_qos_for,
)
from nika.net_env.utils.rack_workload import seed_for
from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BULK_TRANSIT,
    NAT_SESSIONS,
    REALTIME_BULK_CONTENTION,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
    routine_change_processes,
)

SYNC_PORT = 8080
MEDIA_PORT = 16384
REGIONAL_BRANCH = "br1"
# One G.711 call leg: 160-byte payload every 20 ms.
VOICE_KBPS = 69
VOICE_PAYLOAD = 160
VIDEO_PAYLOAD = 1200
# Per-call receive statistics of every softphone.
MEDIA_LOG = "/var/log/softphone.log"
# Per-transfer log of every sync client.
SYNC_LOG = "/var/log/filesync.log"


@dataclass(frozen=True)
class RegionalLoad:
    """br1 HQ-egress load in kbit/s.

    The first ``sync_pcs`` br1 CORP PCs each keep one sync connection at the
    file service's per-connection limit; the video is the HQ call to br1.
    """

    sync_pcs: int
    connection_kbps: int
    video_kbps: int

    @property
    def sync_kbps(self) -> int:
        return self.sync_pcs * self.connection_kbps


# BE ceilings are 4 / 9 / 9 mbit and EF reservations 4 / 7 / 7 mbit
# (``OVERLAY_QOS``). Sync uses 60% of BE, which leaves headroom when the host
# is slow to service the shaper. Each connection runs at 2.4-2.7 mbit, well
# above what a small receive window carries over the ~80 ms branch path. The
# video fits its EF guarantee (RTP/UDP/IP headers add about 2.5%); moved into
# BE, it gets at most its fair share or what the sync leaves, so it loses
# about half of its packets.
REGIONAL_LOAD: dict[TopoSize, RegionalLoad] = {
    "s": RegionalLoad(sync_pcs=1, connection_kbps=2400, video_kbps=3850),
    "m": RegionalLoad(sync_pcs=2, connection_kbps=2700, video_kbps=6800),
    "l": RegionalLoad(sync_pcs=2, connection_kbps=2700, video_kbps=6800),
}

GUEST_ARGS = (
    "--sessions", "48", "--lifetime", "60-240",
    "--burst", "32", "--burst-every", "8-15", "--burst-kb", "2-12",
    "--upload-kb", "256", "--upload-every", "60-180",
)  # fmt: skip
IOT_ARGS = (
    "--sessions", "8", "--lifetime", "300-900", "--telemetry-every", "10",
)  # fmt: skip


def _lan(spec: TopoSpec, site: str, role: str) -> LanSpec | None:
    return next((lan for lan in spec.sites[site].lans if lan.role == role), None)


def _ip(spec: TopoSpec, site: str, role: str, index: int = 0) -> str:
    return str(lan_host_ip(spec.sites[site].site_id, role, index).ip)


def build_workload(spec: TopoSpec, routers: list[str]) -> BusinessWorkload:
    size = spec.size
    load = REGIONAL_LOAD[size]
    regional_corp = _lan(spec, REGIONAL_BRANCH, "corp").host_names
    syncing = regional_corp[: load.sync_pcs]
    if len(syncing) < load.sync_pcs:
        raise ValueError("regional branch has fewer CORP PCs than sync clients")
    if load.sync_kbps > overlay_qos_for(size).rate_mbit * 1000:
        raise ValueError("regional sync exceeds the overlay egress")
    sync_url = f"http://{_ip(spec, 'hq', 'server')}:{SYNC_PORT}/{SYNC_OBJECT}"
    hq_phone = _lan(spec, "hq", "corp").host_names[0]
    hq_phone_ip = _ip(spec, "hq", "corp")

    processes: list[WorkloadProcess] = [
        WorkloadProcess(
            node=_lan(spec, "hq", "server").host_names[0],
            app="file_server",
            command="filedist",
            args=("--port", str(SYNC_PORT), "--rate", f"{load.connection_kbps}k"),
        ),
        WorkloadProcess(
            node=isp_name_for(spec.providers[0]),
            app="web_service",
            command="webapp",
            args=("--port", "80", "--seed", "1"),
        ),
    ]
    exercises = [
        WorkloadExercise(
            REALTIME_BULK_CONTENTION,
            (edge_name_for("hq"), hub_iface_for(REGIONAL_BRANCH, "hq")),
        ),
        # Every sync transfer leaves the file server's LAN interface.
        WorkloadExercise(
            BULK_TRANSIT, (_lan(spec, "hq", "server").host_names[0], "eth0")
        ),
    ]
    hq_peers: list[str] = []
    for branch in spec.branch_names():
        corp = _lan(spec, branch, "corp").host_names
        regional = branch == REGIONAL_BRANCH
        for host in syncing if regional else ():
            processes.append(
                WorkloadProcess(
                    node=host,
                    app="file_sync",
                    command="filesync",
                    args=(
                        sync_url,
                        "--log",
                        SYNC_LOG,
                        "--pause",
                        "0.5-1.5",
                        # Spread the PCs' rounds so their pauses rarely meet.
                        "--start-spread",
                        "60",
                        "--seed",
                        seed_for(host),
                    ),
                )
            )
            exercises.append(WorkloadExercise(BULK_DOWNLOAD, (host,)))
        kbps, payload = (
            (load.video_kbps, VIDEO_PAYLOAD)
            if regional
            else (VOICE_KBPS, VOICE_PAYLOAD)
        )
        leg = f"{kbps}/{payload}"
        hq_peers.append(f"{_ip(spec, branch, 'corp')}:{MEDIA_PORT}/{leg}")
        processes.append(_media(corp[0], [f"{hq_phone_ip}:{MEDIA_PORT}/{leg}"]))
    dc2_phone = _lan(spec, "dc2", "corp").host_names[0]
    voice = f"{VOICE_KBPS}/{VOICE_PAYLOAD}"
    hq_peers.append(f"{_ip(spec, 'dc2', 'corp')}:{MEDIA_PORT}/{voice}")
    processes.append(_media(dc2_phone, [f"{hq_phone_ip}:{MEDIA_PORT}/{voice}"]))
    # One HQ endpoint bridges every call.
    processes.append(_media(hq_phone, hq_peers))

    for site in spec.sites:
        guest = _lan(spec, site, "guest")
        if guest is not None:
            host = guest.host_names[0]
            processes.append(
                WorkloadProcess(
                    node=host,
                    app="web_client",
                    command="webclient",
                    args=(PUBLIC_SERVICE_IP, *GUEST_ARGS, "--seed", seed_for(host)),
                )
            )
            exercises.append(
                WorkloadExercise(NAT_SESSIONS, (edge_name_for(site), guest.prefix))
            )
        iot = _lan(spec, site, "iot")
        if iot is not None:
            host = iot.host_names[0]
            processes.append(
                WorkloadProcess(
                    node=host,
                    app="web_client",
                    command="iot-agent",
                    args=(PUBLIC_SERVICE_IP, *IOT_ARGS, "--seed", seed_for(host)),
                )
            )
    processes += routine_change_processes(routers)
    return BusinessWorkload(processes=tuple(processes), exercises=tuple(exercises))


def _media(node: str, peers: list[str]) -> WorkloadProcess:
    args: list[str] = ["--port", str(MEDIA_PORT), "--log", MEDIA_LOG]
    for peer in peers:
        args += ["--peer", peer]
    return WorkloadProcess(
        node=node,
        app="media_peer",
        command="softphone",
        args=(*args, "--seed", seed_for(node)),
    )
