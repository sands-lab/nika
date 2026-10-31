# Network scenario reference

This reference helps benchmark operators choose and configure a NIKA network scenario. This checkout lists scenario IDs through `nika env list` (including one ID per SNDlib or Topology Zoo ISP topology plus named ISP specials). Most scenarios use one backend; SNDlib ISP topology scenarios support both Kathara and Containerlab.

The [`net_env_pool.py`](../../src/nika/net_env/net_env_pool.py) registry defines the authoritative scenario IDs, backends, tags, and size controls. Backend implementations live under [`net_env/`](../../src/nika/net_env/). Confirm the installed checkout with:

```shell
uv run nika env list
```

## Backend requirements

| Backend | Needs |
| --- | --- |
| Kathará | Docker, Kathará Python package (`uv sync`) |
| Containerlab | Docker, `clab`, `gnmic` |

Install with [`./scripts/install.sh`](../../scripts/install.sh). Installer options: [Installation](installation.md). Overview: root [README](../../README.md#-installation).

Every Containerlab scenario runs Nokia SR Linux, which NIKA configures over gNMI, so `gnmic` is required for all of them. `min3clos` is Containerlab-only. `isp_<topology>` scenarios use Containerlab when you pass `--backend containerlab`.

Containerlab scenarios pull the Nokia SR Linux image and run their Linux hosts on the locally built `nika/base` image. Kubernetes scenarios download k3s and workload images during deployment. `iosxr_simple_bgp` needs a Cisco XRd Control Plane image that you load manually (or via `./scripts/install.sh --with-vendor-images --xrd-tarball …`). See [IOS-XR simple BGP](#ios-xr-simple-bgp-scenario). `routeros_simple_bgp` needs a MikroTik RouterOS `vrnetlab` image (build with `./scripts/install.sh --with-vendor-images`; clone lands under `.nika_cache/vendor/vrnetlab`); see [RouterOS simple BGP](#routeros-simple-bgp-scenario).

### Concurrency and `--batch-size`

Benchmark runs use two concurrency limits (see [`benchmark` settings](configuration.md#benchmark-settings)), both sliding windows that default to `1`:

- `--batch-size` caps light trials: ``s``/``m`` Kathara cases.
- `--heavy-batch-size` caps heavy trials: Containerlab, `k8s_lab` / `llmd_lab` / `iosxr_simple_bgp`, and any topo_size ``l`` case.

Heavy and light trials never run at the same time. With the default `--heavy-batch-size 1`, each heavy session runs alone on the host. Raise it only when the host has capacity for several heavy labs at once, for example `--batch-size 8 --heavy-batch-size 4`.

Why heavy labs need a separate cap:

- `k8s_lab` and `llmd_lab` each run six privileged k3s nodes. Concurrent labs exhaust host inotify capacity and the k3s server exits. `iosxr_simple_bgp` needs the same raised limits. See [Host inotify limits too low](troubleshooting.md#host-inotify-limits-too-low-k3s--xrd).
- Every lab adds entries to the host's shared neighbor table. Large or concurrent labs overflow the kernel default and drop packets at random. See [Host neighbor table overflows](troubleshooting.md#host-neighbor-table-overflows-large-or-concurrent-labs).
- Containerlab scenarios apply their post-deploy SR Linux configuration over gRPC. Under concurrent load the SR Linux management server rejects the keepalives with `ENHANCE_YOUR_CALM` and `too_many_pings`, and `clab deploy` fails. NIKA destroys the partial lab and retries the deploy once. Within one lab, NIKA also passes `clab deploy --max-workers` from `nika.lab.containerlab_max_workers` (default `2`). Lower it if deploy OOMs; see [Containerlab deploy OOM](troubleshooting.md#containerlab-deploy-oom-on-memory-tight-hosts).

## Scenario catalog

| Scenario ID | Backend | Scale control | Options | Network |
| --- | --- | --- | --- | --- |
| `dc_clos` | Kathara | `-s s\|m\|l` | `--device-profile iosxr` (`s`) | FRR eBGP Clos with DNS/HTTP leaf services |
| `campus_lan` | Kathara | `-s s\|m\|l` | `--device-profile iosxr` | Hierarchical campus LAN with DHCP/DNS/LB farm |
| `enterprise_branch` | Kathara | `-s s\|m\|l` | `--device-profile iosxr` | Hub-and-spoke enterprise WAN: provider underlay + WireGuard + eBGP overlay with per-role VRFs |
| `sdn_l3_clos` | Kathara | `-s s\|m\|l` |  | ONOS + OVS L3 Clos with SELECT ECMP |
| `p4_dc_fabric` | Kathara | `-s s\|m\|l` |  | BMv2 `simple_switch_grpc` L3 Clos under P4Runtime; ActionSelector ECMP |
| `p4_dc_gateway` | Kathara | `-s s\|m\|l` |  | Gateway-spine-leaf BMv2 fabric with ECMP, INT-MX, ECN, queues, and flow tracking |
| `iosxr_simple_bgp` | Kathara | Fixed |  | Two Cisco XRd routers with eBGP and two PCs |
| `routeros_simple_bgp` | Kathara | Fixed |  | Two MikroTik RouterOS routers with eBGP and two PCs |
| `isp_<topology>` | Kathara or Containerlab | Fixed metadata `s`/`m`/`l` | Protocol options | One SNDlib graph per scenario ID, compiled to FRR, SR Linux, IOS XR or RouterOS |
| `isp_<zoo topology>` | Kathara | Fixed metadata `s`/`m`/`l` | Protocol options | One Topology Zoo graph per scenario ID, compiled to FRR, IOS XR or RouterOS; not a benchmark candidate |
| `isp_abilene_ebgp_rpki` / `isp_geant_ebgp_rpki` | Kathara | Fixed | — | Named eBGP + offline RPKI overlays |
| `isp_abilene_ebgp_rtbh` / `isp_dfn-bwin_ebgp_rtbh` | Kathara | Fixed | — | Named eBGP + RTBH blackhole overlays |
| `min3clos` | Containerlab | Fixed |  | Five-node SR Linux eBGP Clos |
| `k8s_lab` | Kathara | Fixed |  | FRR fat-tree with a six-node k3s cluster |
| `llmd_lab` | Kathara | Fixed |  | L2 k3s cluster with simulated llm-d inference |

Six scenario IDs accept `s`, `m`, or `l`. Pass a size when you deploy one:

```shell
uv run nika env run dc_clos -s s
```

Fixed scenarios reject a size. Each SNDlib ISP topology is its own scenario ID (for example `isp_abilene`); size is metadata for benchmark sampling only.

## Data-center Clos scenario

NIKA builds one Clos fabric from code. Each router runs FRR eBGP. Inter-router links use `/31` networks from `172.16.0.0/16`, and leaf access networks use `10.<pod>.<leaf>.0/24`.

```text
                    super-spine(s)
                  /       |       \
             spine(s) ... spine(s)
               /  \           /  \
            leaf  leaf  ...  leaf  leaf
             |      |          |      |
             +------ service endpoints
```

| Size | Super-spines and pods | Spines per pod | Leaves per pod |
| --- | ---: | ---: | ---: |
| `s` | 1 | 2 | 2 |
| `m` | 2 | 4 | 4 |
| `l` | 4 | 8 | 8 |

Super-spine `n` uses AS 65000+`n`, spines use AS 651xx, and leaves use AS 652xx. Because the super-spines have distinct ASNs, external clients of different pods reach each other as well as the DNS and web services. Each pod's BIND server is authoritative only for its own pod zone, and clients fall back across the pod servers.

Each pod has one authoritative BIND server, HTTP servers on the remaining leaves, and an external client on the super-spine (`192.168.<pod>.0/24`). Clients resolve names such as `web0.pod0`. Use this when the failure needs DNS or HTTP observability.

Business traffic: the users behind each client browse every pod's web servers by name every few seconds and download `large.bin` (2 MB) from one of them every few minutes.

```shell
uv run nika env run dc_clos -s s
```

## Campus LAN scenario

NIKA builds one campus LAN fabric: a three-router core triangle, distribution and access tiers, user LANs, and a server farm on core3. FRR advertises routed links and services through OSPF. Routed uplinks use `/31` networks from `172.16.0.0/16`; user LANs use `10.<core>.<distribution>.0/24`.

```text
user PCs -- access switches -- distribution routers
                                      \       /
                                  core triangle
                                        |
                              server-access device
                               /    |     |    \
                             DNS  HTTP  DHCP  load balancer
```

| Size | Distribution routers | Access switches | User hosts |
| --- | ---: | ---: | ---: |
| `s` | 2 | 2 | 2 |
| `m` | 4 | 8 | 16 |
| `l` | 8 | 32 | 128 |

Hosts acquire addresses from `dhcp_server`; distribution routers relay DHCP. Dist/access names stay `router_dist_*` / `server_access_router`.

The farm adds an NGINX load balancer at `web99.local` and three backend webs on `20.200.0.0/24`. Each `webN.local` server runs NGINX, which serves the campus software repository over HTTP and HTTPS and proxies other paths to the mock page service.

Every PC browses `web0`-`web3.local` and `web99.local` every few tens of seconds. The teaching-lab PCs, every PC in the `10.2.<dist>.0/24` access blocks behind `router_core_2`, refresh their software catalog every few minutes by downloading the gzip-encoded `https://web0.local/packages/catalog.txt` (64 MiB decoded), trusting the repository through the campus CA. Each PC also has a local archive job configured with one worker in `/etc/archive-job.json`.

Verification checks OSPF adjacency, cross-branch reachability, DNS, and HTTP.

```shell
uv run nika env run campus_lan -s s
```

## Enterprise Branch VPN scenario

### `enterprise_branch`

NIKA builds a multi-site enterprise WAN from one production template: HQ and a secondary DC hub, branch sites, and dual provider underlays. Every size keeps the same dual providers, dual hubs, WAN redundancy, and WireGuard+eBGP overlay. Size scales branch count and hosts per LAN. The `m` and `l` sizes add an IOT VRF, which increases the number of business domains alongside the replicated VLANs.

Each site has business LANs bound into Linux VRFs on the Site Edge (`vrf_corp`, `vrf_server`, `vrf_guest`, and on `m`/`l` `vrf_iot`), so the Docker host needs the `vrf` kernel module (see [Host kernel lacks the vrf module](troubleshooting.md#host-kernel-lacks-the-vrf-module-enterprise_branch)). Sites do not mesh over physical links. Edges attach to both providers for IP underlay reachability between tunnel endpoints only. WAN PE links, WireGuard tunnels, and eBGP sessions stay in the default VRF.

Site Edge routers terminate WireGuard site-to-site tunnels and run eBGP over those tunnels to exchange authorized business prefixes (CORP, SERVER). FRR imports those overlay prefixes into the matching business VRFs. SERVER is a shared-services domain: CORP and SERVER exchange prefixes through explicit route leaking only. GUEST and IOT stay local (default route via the provider with NAT) and are not advertised in overlay BGP. Cross-site traffic follows `LAN VRF → Site Edge → VPN tunnel → Provider underlay → Remote Edge → Remote LAN VRF`. Branch-to-branch traffic hairpins a hub. Guest and IOT isolation is a design property, not a fault, and the underlay delay below puts branch-to-hub RTT near 80 ms and branch-to-branch RTT near 160 ms. HQ and DC2 also peer directly over dual-provider WireGuard+eBGP. Inbound local preference (200 primary, 100 backup) makes each branch's isp1 tunnel to HQ the active path, so branch traffic to DC2 also transits HQ; the isp2 HQ tunnel and the direct DC2 tunnel stay established as backups.

```text
  HQ CORP/SERVER/GUEST[/IOT] -- hq_edge ==WG+eBGP== brN_edge -- Branch CORP/GUEST[/IOT]
  DC2 CORP/SERVER/GUEST[/IOT] -- dc2_edge ==WG+eBGP== brN_edge
                               |                    |
                            isp1_core            isp2_core
  hq_edge ==WG+eBGP== dc2_edge   (hub interconnect, dual provider)
```

| Size | Hubs | Branches | Providers | VRFs (hub / branch) | Hosts per LAN | Overlay per branch |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| `s` | HQ + DC2 | 2 | 2 | corp,server,guest / corp,guest | 1 | primary HQ (isp1) + backup HQ (isp2) + backup DC2 (isp1) |
| `m` | HQ + DC2 | 4 | 2 | + iot on all sites | 2 | same |
| `l` | HQ + DC2 | 8 | 2 | + iot on all sites | 4 | same |

Every Site Edge is dual-homed to both providers. SERVER stays one host per hub (HTTP anchor). Enterprise LANs use `10.<site_id>.<role>.0/24` (role octet `10` CORP, `20` SERVER, `30` IOT, `40` GUEST). Provider PE links use `100.64.0.0/16`. Tunnel addressing uses `172.30.0.0/16`. HQ ASN is `65000`; branches use `65001+`; DC2 uses `65010`.

Healthy Site Edges preserve DSCP on CORP overlay traffic and shape each WireGuard overlay egress with a dual-class HTB queue: EF (DSCP 46) gets a reserved high-priority class; CS0/BE is hard-capped at the rest of the link and served by `fq_codel`, as WAN-edge QoS pairs fair queueing and AQM with the default class (Cisco `fair-queue` in `class-default`, OpenWrt SQM `simple.qos`). Bulk transfers keep no standing queue and pings skip ahead of them, while a flow sending above its fair share of the class is dropped. Per-size overlay egress capacity is `s` 8 mbit (EF 4), `m` and `l` 16 mbit (EF 7). Size `l` keeps the `m` rate because its eight branches share the same hubs and underlay.

GUEST and IOT LANs break out to the Internet locally. Each Site Edge source-NATs them to its own routed public address `198.18.<site_id>.10` on the primary WAN (br1 also holds `198.18.1.11`), and the Linux server `internet_web` on a LAN of `isp1_core` (`198.51.100.8/29`) hosts the public web service `198.51.100.10`. It runs on every device profile, including XRd providers.

The scenario runs [business traffic](#business-traffic) in every task:

| Traffic | Endpoints | Load |
| --- | --- | --- |
| File sync | `hq_srv:8080` (`filedist`, per-connection limit) to the first br1 CORP PC (`s`) or first two (`m`/`l`), one connection each, back to back; each transfer is logged to `/var/log/filesync.log` | `s` 2.4 / `m` 2.7 / `l` 2.7 mbit per connection: br1 total `s` 2.4 / `m` 5.4 / `l` 5.4 mbit, 60% of the 4 / 9 / 9 mbit BE ceiling of `hq_edge` `wg_br1` |
| Calls (`softphone`, DSCP EF) | `hq_corp_pc` to the first CORP PC of every branch and DC2 | br1 video link `s` 3.85 / `m` 6.8 / `l` 6.8 mbit each way, within the EF reservation; G.711 voice elsewhere. Each softphone logs per-call receive packets, loss, and jitter every 10 s to `/var/log/softphone.log` |
| Guest Internet (`webclient`) | First GUEST host of every site to `198.51.100.10` through the NAT | 48 long-poll sessions, page loads of 32 parallel 2-12 KB fetches every 8-15 s, a 256 KB upload every 1-3 min |
| IoT telemetry (`iot-agent`, `m`/`l`) | First IOT host of every site to `198.51.100.10` through the NAT | 8 sessions posting every 10 s |

### Underlay WAN propagation delay

Each Site Edge↔provider underlay attachment applies one-way `netem delay 20ms` on both PE ends. A branch→HQ path therefore sees about 40 ms one-way and about 80 ms RTT (plus WireGuard), with no added loss or jitter. Values come from the [Cisco Catalyst SD-WAN Small Branch Design Case Study](https://www.cisco.com/c/en/us/td/docs/solutions/CVD/SDWAN/cisco-sdwan-casestudy-smallbranch.html) (American GasCo):

| Case-study evidence | Value | Lab use |
| --- | --- | --- |
| Table 22 Bulk-Data SLA | 300 ms RTT ceiling | Healthy lab RTT stays well below |
| Table 23 `SLA_BUSINESS_DATA` | 400 ms RTT, 2% loss, 100 ms jitter | Business-data AAR ceiling; BFD metrics are RTT |
| Table 22 Transactional-Data SLA | 50 ms RTT | Tighter interactive apps; bulk paths may exceed this |
| Table 6 Type 1–2 site bandwidth | up to 50 / 150 Mbps | Overlay `s`/`m`/`l` = 8 / 16 / 16 mbit, scaled down for the lab |
| Geography | Atlanta HQ, southeastern US stores | Regional branch–DC path |

This delay is scenario fidelity for WAN BDP. It is not part of any failure root cause.

SERVER-role hosts serve static HTTP objects at `http://<server>/small.bin` (16 KB) and `http://<server>/large.bin` (1 MB, a few seconds over a healthy branch path), plus the 32 MB `projects.tar` the br1 file sync pulls. Host containers run privileged so TCP receive-buffer sysctls (`net.ipv4.tcp_rmem`, `tcp_moderate_rcvbuf`) are writable for receiver-window faults. Docker typically keeps `net.core.rmem_max` read-only; `tcp_rmem` max remains the effective TCP ceiling when it is below the host `rmem_max`.

Use this scenario for underlay vs overlay diagnosis, VRF business isolation, hub-and-spoke VPN reachability, eBGP path preference with backup sessions at every scale, overlay-egress DSCP/QoS faults, and receiver-side TCP receive-window bottlenecks on branch–HQ paths. Verification covers VRF devices on every edge, every designed tunnel (underlay reachability, WireGuard, BGP both sides), per-VRF RIB contents (CORP sees CORP+SERVER leak; GUEST/IOT do not), hub interconnect, every branch CORP↔HQ CORP and CORP↔DC2 CORP plus HTTP to both hub SERVERs, every branch pair via the corp VRF overlay path, every provider without enterprise prefixes, GUEST/IOT isolation from CORP, every backup BGP session with primary-path preference, and HTB EF/BE classes on every WireGuard overlay egress.

The ID `rip_small_internet_vpn` names the original 0.1.0 RIP mini-Internet lab, a separate lab that only release 0.1.0 uses. See [Run release 0.1.0](../compat/release-0.1.0.md).

Boundary: `campus_lan` is a single-campus L3 network; `dc_clos` is a data-center fabric; `isp_*` scenarios are carrier IGP/BGP itself. This scenario is enterprise multi-site WAN with encrypted overlay and per-role VRFs.

```shell
uv run nika env run enterprise_branch -s s
```

## SDN scenarios

### `sdn_l3_clos`

Symmetric leaf-spine L3 Clos under centralized ONOS control. Switches are Open vSwitch (`fail-mode=secure`, OpenFlow 1.3). The out-of-band control network is `172.31.0.0/16` with ONOS at `172.31.0.100:6653`. On deploy, `ensure_nika_docker_images` builds `nika/onos` when missing or outdated and pulls `kathara/sdn` when missing. `nika/onos` is a multi-arch image: `kathara/base` plus a host-arch Temurin 11 JRE, with the ONOS Java tree copied from pinned `onosproject/onos:2.7-latest` (amd64-only upstream). That keeps Kathara's architecture check happy on Linux arm64 and amd64 without qemu. Each leaf owns rack prefix `10.0.<leaf>.0/24` with gateway `.1` and a shared virtual router MAC. Endpoints start at `.11` (one `web_*` nginx endpoint plus `client_*` workers per leaf). Business traffic: clients load pages and `small.bin` from the web servers every few seconds and download `large.bin` (16 MB) every few minutes. A host-side fabric manager installs proactive IPv4 forwarding and OpenFlow `SELECT` ECMP groups (stable five-tuple hash). No STP and no `NORMAL` learning fallback.

```text
                    ONOS
                     |
            OOB control network
                     |
          spine_1 ... spine_N
           |\           /|
           | \         / |
         leaf_1 ...... leaf_M
          | |          | |
       web/client   web/client
```

| Size | Spines | Leaves | Endpoints per leaf |
| --- | ---: | ---: | ---: |
| `s` | 2 | 4 | 2 |
| `m` | 4 | 8 | 4 |
| `l` | 8 | 16 | 4 |

Deploy starts containers, waits for ONOS device discovery over live OpenFlow sessions, then installs proactive L3 flows and SELECT ECMP groups through the ONOS REST API so the controller keeps owning forwarding state. Controllers stay attached. Verification checks live OpenFlow sessions, topology consistency, addressing, cross-rack ICMP/HTTP, ECMP group shape, and controller vs OVS dataplane evidence. Diagnosis tools for this lab are documented under [MCP servers](../agents/mcp-servers.md#sdn-kathara_sdn_mcp_server).

```shell
uv run nika env run sdn_l3_clos -s s
```

## P4 scenarios

The two Kathara P4 scenarios start BMv2 `simple_switch_grpc` with no local table file and configure forwarding through P4Runtime.

### `p4_dc_fabric`

Symmetric leaf-spine L3 Clos of BMv2 `simple_switch_grpc` switches under a NIKA P4Runtime fabric manager. The out-of-band control network is `172.31.0.0/16` with `fabric_mgr` at `172.31.0.101`. Switches start with `--no-p4` and listen on `:9559`. Deploy compiles the v1model IPv4 fabric program once on a leaf, then `SetForwardingPipelineConfig` plus table/group writes program every switch. Role is table state: same-rack `/32` to the host port, remote `/24` via ActionSelector ECMP over all spines. Endpoints use `/32` addresses on rack `10.0.<leaf>.0/24`, send all IPv4 via gateway `.1`, and keep a permanent neighbor for the shared virtual router MAC. Business traffic: clients load pages and `small.bin` from the web servers and download `large.bin` (2 MB) every few minutes; `web_2` runs a search front end that about once a second queries up to four index shards on other web servers (UDP 11311), which all answer at once with 24 datagrams of 1400 bytes. Build `nika/fabric-controller` on deploy when missing or outdated (the image name must not contain `p4`; NIKA classifies `"p4" in image` as BMv2).

```text
                 fabric_mgr
                      |
             OOB control network
                      |
           spine_1 ... spine_N
            |\           /|
            | \         / |
          leaf_1 ...... leaf_M
           | |          | |
        web/client   web/client
```

| Size | Spines | Leaves | Endpoints per leaf |
| --- | ---: | ---: | ---: |
| `s` | 2 | 4 | 2 |
| `m` | 4 | 8 | 4 |
| `l` | 8 | 16 | 4 |

Verification checks `simple_switch_grpc`, OOB reachability, P4Runtime Read vs intent, same-rack and sparse cross-rack ICMP, HTTP to a remote web, and multi-flow ECMP counters on at least two spines. Diagnosis tools for this lab are documented under [MCP servers](../agents/mcp-servers.md#p4--bmv2-kathara_bmv2_mcp_server).

```shell
uv run nika env run p4_dc_fabric -s s
```

The ID `p4_counter` names the original 0.1.0 L2 counter lab, a separate lab that only release 0.1.0 uses. See [Run release 0.1.0](../compat/release-0.1.0.md).

### `p4_dc_gateway`

This benchmark scenario connects one external client to each gateway, fully meshes gateways to spines and spines to leaves, and connects two HTTP services to each leaf. Every switch also joins a telemetry LAN, and an isolated OOB network carries P4Runtime traffic.

| Size | Gateways | Spines | Leaves | External clients | HTTP services |
| --- | ---: | ---: | ---: | ---: | ---: |
| `s` | 2 | 2 | 2 | 2 | 4 |
| `m` | 4 | 4 | 4 | 4 | 8 |
| `l` | 8 | 8 | 8 | 8 | 16 |

The shared v1model pipeline provides IPv4 LPM, five-tuple ActionSelector ECMP, packet and byte counters, fixed INT-MX source and sink processing, four-position SYN and non-SYN counting Bloom filters, per-port ECN thresholds, and queue occupancy.

BMv2 sends packets as fast as its CPU allows, so its egress queue stays empty. Each egress port therefore keeps a virtual queue that drains one packet per 16384 µs (about 61 packets per second) and holds at most 64 packets. The `queue_occupancy` register reports that depth, and ECN marks ECT packets when the depth reaches the port threshold. The virtual queue never drops packets.

Each gateway serves the TCP VIP `20.0.0.1:80` as a SilkRoad-style stateful L4 load balancer. A versioned 64-bucket DIP pool picks a backend for a new connection, and a gateway-local agent (`lb_agent.py`) learns the connection about 5 ms after its SYN by pinning the five-tuple to that DIP in the 256-entry `lb_conn_table` (RST, FIN, and idleness remove it). The agent reads its learning delay and connection limit (`max_connections`, 256) from `/etc/lb-agent/config.json`. A connection it cannot pin because the table is full stays mapped by the pool hash alone. The two backends of the VIP (`service_1_1`, `service_1_2`) are meant to be reached through `20.0.0.1:80`. The gateway rewrites the source of a reply to the VIP only for connections it translated, so a client that connects to a backend address directly talks to an ordinary host.

The agent also performs a routine pool update (a VIP version flip, as during deploys or scaling) on every gateway every 60 to 180 seconds, after waiting up to 100 ms for pending connections to be learned. Learned connections keep their DIP across it. It logs each update, a full table, and any reset of a connection it has not learned, in `/tmp/lb-agent.log` on the gateway.

External clients reach their gateway over a provider access circuit with a 1400-byte path MTU (the gateway's `egress_mtu` entry for the client port). For a larger packet with DF set toward a client, the gateway returns ICMP Fragmentation Needed to the sender, so services rely on path MTU discovery for large downloads. The fabric is north-south only: gateways and leaves carry routes for clients and for services, so client-to-client and cross-leaf service-to-service pairs have no path by design.

Users at each client site keep keep-alive browsing sessions to the VIP (some reopen every 10 to 30 seconds), load `small.bin` from the services directly, and download `large.bin` (2 MB) through the VIP or from a service every few minutes. `service_1_1` runs a status collector that every few seconds polls the agent at every client site (UDP 11311), and all sites answer at once with 24 datagrams of 1400 bytes. The fabric controller logs every P4Runtime Write it sends, with the status of each update, in `/var/log/p4rt/controller.log` on `fabric_mgr`. BMv2 and INT MCP tools for this lab are documented under [MCP servers](../agents/mcp-servers.md#p4--bmv2-kathara_bmv2_mcp_server) and [Telemetry](../agents/mcp-servers.md#telemetry-kathara_telemetry_mcp_server).

```shell
uv run nika env run p4_dc_gateway -s s
uv run nika traffic run burst --sources client_1,client_2 --destination service_1_1 --protocol tcp --rate 10M --packet-size 1200 --duration 10 --seed 42
```

## IOS-XR simple BGP scenario

### `iosxr_simple_bgp`

Two Cisco XRd Control Plane routers peer over eBGP, each with one Linux PC. The PCs run the same [business traffic](#business-traffic) as `min3clos` across both routers. Cisco licensing blocks redistributing or auto-building the image like `nika/*`, so you load and tag it before deploy.

One-shot from a local Cisco tarball (no public download URL):

```shell
./scripts/install.sh --with-vendor-images \
  --xrd-tarball /path/to/xrd-control-plane-container-x86_64-<version>.tgz
```

Or place the file at `.nika_cache/vendor/xrd-*.tgz` and run `./scripts/install.sh --with-vendor-images`. Manual steps:

1. Download the XRd Control Plane container tarball from Cisco (CCO account with an XRd Control Plane entitlement, for example through Cisco Software Download or Cisco Modeling Labs). The file looks like `xrd-control-plane-container-x86_64-<version>.tgz`.

2. Load and tag it to the image reference in [`common.py`](../../src/nika/net_env/utils/iosxr/common.py) (`IMAGE`, currently `ios-xr/xrd-control-plane:26.2.1`). The CCO download wraps the image with signing files, so extract the inner `*.dockerv1.tgz` first. For a different XRd version, retag as `26.2.1` or change that constant:

```shell
tar -xzf xrd-control-plane-container-x86_64-<version>.tgz --wildcards '*.dockerv1.tgz'
docker load -i xrd-control-plane-container-x64.dockerv1.tgz
docker tag <loaded-repo>:<loaded-tag> ios-xr/xrd-control-plane:26.2.1
docker images | grep xrd-control-plane
```

If the tag is missing, `nika env run iosxr_simple_bgp` raises a `RuntimeError` with the same `docker load` / `docker tag` steps instead of deploying a broken lab.

3. Raise host inotify limits for XRd (IOS XR >= 7.9.2). Follow [Host inotify limits too low](troubleshooting.md#host-inotify-limits-too-low-k3s--xrd).

4. Deploy:

```shell
uv run nika env run iosxr_simple_bgp
```

Give the host at least 8 vCPUs and 16 GB of RAM. On a 4 vCPU / 8 GB host, one of the two routers crashes during boot (`Bus error` in `/var/log/startup.log` inside the container) and the lab never passes startup verification.

Each router runs privileged with IPv6 enabled (Kathara device metadata in `lab.py`). XRd ZTP can briefly race the container network namespace at first boot; the router startup scripts retry config apply until that clears, so a slow first boot is expected.

## Native IOS XR routers

To study the same routing design on Cisco IOS XR, pass `--device-profile iosxr`. NIKA keeps the environment's topology, addressing, hosts and services, and runs the routers of the eligible roles as XRd Control Plane containers with native IOS XR configuration. Nodes in other roles keep their FRR/Linux design, so tasks and ground truth are unchanged. Readiness and verification read XR's own `show` output on the XR routers.

```shell
uv run nika env run isp_arpanet196912 --device-profile iosxr --igp isis --bgp-mode ibgp_rr
uv run nika env run dc_clos -s s --device-profile iosxr
uv run nika env run campus_lan -s s --device-profile iosxr
uv run nika env run enterprise_branch -s s --device-profile iosxr
```

| Environment | Routers on IOS XR | Routing on IOS XR | Sizes |
| --- | --- | --- | --- |
| `isp_<topology>` (SNDlib and Topology Zoo) | All | OSPF or Level-2 IS-IS; BGP `none`, `ibgp_rr` or `ebgp` with the business-prefix allow-list | Graphs that pass memory admission |
| `dc_clos` | All | eBGP with multipath across spines | `s` |
| `campus_lan` | Core routers | Multi-area OSPF with the FRR distribution and server gateways | `s`, `m`, `l` |
| `enterprise_branch` | Provider routers | Static underlay routes with the provider WAN delay | `s`, `m`, `l` |

Campus distribution and server gateways stay FRR because they bridge access ports into a routed interface and relay DHCP; XRd Control Plane cannot route a bridged interface. Enterprise site edges stay FRR because they terminate WireGuard, VRFs and NAT, which XRd does not provide. The RPKI and RTBH ISP specials remain FRR only.

Each XRd router uses about 2 GiB of memory and has a 3 GiB limit, so NIKA checks available memory before it creates any container and rejects larger `dc_clos` sizes. Install the image and raise inotify limits as described for [`iosxr_simple_bgp`](#ios-xr-simple-bgp-scenario). XR boots and applies its configuration in three to five minutes, which the readiness check waits for.

The XRd image has no `tc`. XR traffic still passes the queueing disciplines of the container interfaces, so NIKA applies the enterprise provider WAN delay from a short-lived helper container in the router's network namespace. Link-quality failures that run `tc` inside an XR router container fail to inject; target the FRR/Linux side of the link instead.

XRd does not observe the carrier state of its Linux interfaces. After `link_down`, `show interfaces` in XR still reports the port as up; the IGP or BGP session fails when its hold time expires (40 seconds for OSPF, 30 seconds for IS-IS, 180 seconds for BGP). Use `ip link` in the router container to see the carrier state.

## RouterOS simple BGP scenario

### `routeros_simple_bgp`

Two MikroTik RouterOS Cloud Hosted Router (CHR) routers peer over eBGP, each with one Linux PC. The PCs run the [business traffic](#business-traffic) of `min3clos` across both routers, scaled to the [RouterOS send cap](#run-other-environments-on-routeros-routers) (600 and 300 kbit/s of replication). RouterOS via `vrnetlab` boots a full QEMU VM inside the container (unlike XRd's native container process), and MikroTik's licensing blocks redistributing or auto-building the image like `nika/*`, so you build and tag it before deploy.

Preferred path (downloads CHR from MikroTik, clones `hellt/vrnetlab` into `.nika_cache/vendor/vrnetlab`, and builds the image). Needs `/dev/kvm`:

```shell
./scripts/install.sh --with-vendor-images
```

Override the cache root with `NIKA_VENDOR_CACHE` if needed. If `vrnetlab/mikrotik_routeros:7.21.5` already exists locally, the installer skips the download and build.

Manual fallback (same cache layout as the installer):

1. Download a CHR image from [mikrotik.com/download](https://mikrotik.com/download) (the `.vmdk` variant for x86, or `.vdi` for arm64). Match the image architecture to the Docker host. Direct links for 7.21.5: `https://download.mikrotik.com/routeros/7.21.5/chr-7.21.5.vmdk.zip` (amd64) and `https://download.mikrotik.com/routeros/7.21.5/chr-7.21.5-arm64.vdi.zip` (arm64).

2. Clone `hellt/vrnetlab` into the vendor cache and build from `mikrotik/routeros` (its base image already ships `sshpass`, which NIKA execs alongside `ssh` to reach RouterOS's internal management API — see [`routeros_api.py`](../../src/nika/service/lab/routeros_api.py)). Do not change the image's `ENTRYPOINT`/`--connection-mode`: the scenario passes `--connection-mode macvtap` as a Kathara machine argument at deploy time instead, because Kathara attaches interfaces before the container starts and vrnetlab's default `vrxcon`/`tc` datapaths expect a data interface to appear only after boot.

```shell
mkdir -p .nika_cache/vendor
git clone --depth 1 https://github.com/hellt/vrnetlab .nika_cache/vendor/vrnetlab
# copy the downloaded CHR .vmdk/.vdi into .nika_cache/vendor/vrnetlab/mikrotik/routeros
cd .nika_cache/vendor/vrnetlab/mikrotik/routeros
make docker-image
```

3. Tag the built image to match the image reference in [`common.py`](../../src/nika/net_env/utils/routeros/common.py) (`IMAGE`, currently `vrnetlab/mikrotik_routeros:7.21.5`). For a different RouterOS version, retag as `7.21.5` or change that constant:

```shell
docker tag <built-tag> vrnetlab/mikrotik_routeros:7.21.5
docker images | grep routeros
```

If the tag is missing, `nika env run routeros_simple_bgp` raises a `RuntimeError` that points at `./scripts/install.sh --with-vendor-images` instead of deploying a broken lab.

4. RouterOS/vrnetlab boots a full QEMU VM per router, so the host needs KVM / nested virtualization available (`/dev/kvm` present; nested virtualization enabled at the hypervisor level if the host itself is a VM). Without KVM, boot is dramatically slower or may not complete.

5. Deploy:

```shell
uv run nika env run routeros_simple_bgp
```

Boot is slower than the FRR and XRd scenarios because each router boots a nested VM; the scenario's verification window accounts for this, so a slow first boot is expected and not a failure.

### Run other environments on RouterOS routers

To study the same routing design on MikroTik RouterOS, pass `--device-profile routeros`. First build the local `vrnetlab/mikrotik_routeros:7.21.5` image and make sure the host has KVM, as described in [RouterOS simple BGP](#routeros-simple-bgp-scenario). As with [native IOS XR routers](#native-ios-xr-routers), NIKA keeps the environment's topology, addressing, hosts and services and runs the routers of the eligible roles as RouterOS VMs with native configuration. Their container interfaces keep their names, so tasks and ground truth are unchanged.

```shell
uv run nika env run isp_arpanet196912 --device-profile routeros --igp isis --bgp-mode ibgp_rr
uv run nika env run dc_clos -s s --device-profile routeros
uv run nika env run campus_lan -s s --device-profile routeros
uv run nika env run enterprise_branch -s s --device-profile routeros
```

| Environment | Routers on RouterOS | Routing on RouterOS | Sizes |
| --- | --- | --- | --- |
| `isp_<topology>` (SNDlib and Topology Zoo) | All | OSPF or Level-2 IS-IS; BGP `none`, `ibgp_rr` or `ebgp` with the business-prefix allow-list | Graphs that pass memory admission |
| `dc_clos` | All | eBGP with one best path per prefix | `s` |
| `campus_lan` | Core, distribution and server gateways | Multi-area OSPF, bridged gateways and DHCP relay | `s` |
| `enterprise_branch` | Site edges | Per-role VRFs, WireGuard, overlay eBGP, guest NAT and EF/BE overlay QoS | `s` |

Each router is a VM that needs about 1 GiB, so NIKA checks available memory before it creates any container. A router supports at most 30 data ports. The RPKI and RTBH ISP specials remain FRR only.

Agents run RouterOS commands with the `routeros_exec` tool of [`kathara_routeros_mcp_server`](../agents/mcp-servers.md#use-dedicated-diagnosis-tools). RouterOS `ether1` is vrnetlab's management port. In an ISP environment, container interface `ethN` is RouterOS `ether(N+1)`; in the other environments, `ethN` is `ether(N+2)`.

Link-down failures behave as on FRR: QEMU does not pass a container interface's link state to the VM, so a watcher in each router container mirrors it to the RouterOS port, and routes over a downed link are withdrawn at once.

Link-quality failures, such as `link_capacity_bottleneck`, are not supported on a link between two RouterOS routers and raise `RuntimeCapabilityError`. Target a link with at least one FRR or Linux endpoint instead.

The CHR image runs with the free license, which sends at most 1 Mbit/s on each interface. Traffic that crosses RouterOS routers is sized below that cap: paced business transfers use at most 60% of it and a [background traffic profile](#background-traffic-profiles) at most 15%, which leaves headroom for hub edges that carry several tunnels on one port. In `enterprise_branch`, the overlay QoS shrinks by the same factor as the sync and video it carries (600 kbit/s with a 300 kbit/s EF reservation at size `s`), so the QoS behaves as it does on FRR. Downloads, such as the `campus_lan` catalog, keep their size and are read at a limited rate, so they take longer. Paths that carry less than 1.5 Mbit/s of bulk traffic are not targets of failures that need `bulk_transit` traffic, and failures that need a faster path, such as `receiver_resource_contention` and `link_packet_corruption`, have no RouterOS tasks.

Two routing behaviors differ from FRR:

* RouterOS installs one best BGP path, so `dc_clos` traffic uses a single spine instead of FRR's equal-cost multipath.
* RouterOS advertises passive IS-IS interfaces, such as host-facing links, with metric 0. Route selection is unchanged because each such prefix has one origin.

If RouterOS rejects a startup command, the environment fails immediately with the router name and RouterOS's import error instead of waiting for the verification deadline.

On a RouterOS site edge, the WAN stays in `main`, and the WireGuard tunnels and their eBGP sessions run in an `overlay` VRF. BGP VPN route targets leak overlay routes into `vrf_corp` and `vrf_server` and export their LANs back. This matches the FRR edge, where business VRFs import the default VRF's BGP routes but not each other's LANs, and guest VRFs import nothing. Overlay QoS uses mangle packet marks and a queue tree with the same EF and BE rates as the FRR edge's HTB classes. The WAN delay stays on the container links.

Failures that change router state apply the same root cause with RouterOS commands: WireGuard keys and allowed addresses, DSCP remarking, source NAT, BGP filters, AS numbers, advertisements and hijacks, blackhole routes, interface MTU and ICMP filters. `frr_service_down` is rejected on RouterOS routers, which have no separate routing daemon to stop.

## SNDlib ISP scenarios

### `isp_<topology>`

NIKA imports SNDlib XML through a backend-neutral topology model. It converts each SNDlib node into a router, each physical link into a point-to-point `/31`, and each router into an attachment point for a `pc_<router>` traffic stub. Kathara renders the plan as FRR routers. Containerlab renders the same plan as Nokia SR Linux routers. SNDlib supplies topology, link attributes, and demand matrices; NIKA supplies the IGP and optional BGP policy presets.

Each vendored SNDlib graph is a separate scenario ID (`isp_abilene`, `isp_france`, …). Topology identity is the scenario name. Relative size `s` / `m` / `l` is fixed metadata for benchmark sampling (by node-count tier), not a CLI flag.

Abilene is one example. The map shows its 12 routers and 15 links; NIKA also attaches a `pc_<router>` traffic host to each router.

<img src="../../assets/images/abilene-sndlib-topology.svg" alt="Abilene SNDlib backbone with 12 routers and 15 links across the United States" width="800">

Map: [TopoHub's Abilene topology](https://www.topohub.org/?topology=sndlib%2Fabilene), based on [SNDlib](https://sndlib.put.poznan.pl/networks.overview.action). © Piotr Jurkiewicz; [MIT license](../../assets/images/abilene-sndlib-topology.LICENSE). The topology matches the vendored [`abilene.xml`](../../src/nika/net_env/isp/topologies/abilene.xml).

```shell
# Default: Kathara, IS-IS, constant metric 10, no BGP
uv run nika env run isp_abilene

uv run nika env run isp_abilene --igp ospf \
  --metric-strategy routing_cost --bgp-mode ibgp_rr

uv run nika env run isp_france --backend containerlab \
  --device-profile nokia_srlinux --igp isis

uv run nika env run isp_abilene --device-profile routeros --igp isis
```

| Control | Accepted values | Default | Effect |
| --- | --- | --- | --- |
| `--backend` | `kathara`, `containerlab` | `kathara` | Selects FRR or SR Linux rendering |
| `--device-profile` | `frr`, `nokia_srlinux`, `iosxr`, `routeros` | Derived from backend | Router platform. `iosxr` and `routeros` run on Kathara and need their images: see [native IOS XR routers](#native-ios-xr-routers) and [RouterOS routers](#run-other-environments-on-routeros-routers) |
| `--igp` | `isis`, `ospf` | `isis` | Selects the IGP compiler |
| `--metric-strategy` | `constant`, `routing_cost`, `inv_capacity` | `constant` | Maps SNDlib link data to IGP metrics |
| `--constant-metric` | Positive integer | `10` | Sets constant and fallback metrics |
| `--bgp-mode` | `none`, `ibgp_rr`, `ebgp` | `none` | Adds a NIKA-defined BGP preset |

`ibgp_rr` uses AS 65000, selects up to two route reflectors, and originates up to three TEST-NET business prefixes. Originators hold these prefixes on a `lo1` dummy interface that no IGP runs on, so only BGP carries them. `ebgp` partitions the physical graph into up to three connected AS regions. Each AS uses one route reflector, and each physical AS boundary carries an eBGP session. The IGP forms adjacencies inside each AS and treats inter-AS interfaces as passive. ASes exchange only the originated business prefixes, so each `pc_<router>` stub subnet is internal to its AS: stubs reach stubs in their own AS, not in other ASes.

Every ISP environment runs [business traffic](#business-traffic) that follows the topology's SNDlib demand matrix:

- **Data-set syncs.** Each `pc_<router>` stub publishes an 8 MB data set over HTTP on port 8080 and repeatedly syncs the data set of the PoP it has the largest demand from; a PoP that no stub pulls from is synced by its own largest-demand destination. Each transfer is paced at 0.3–2.5 Mbit/s, scaled from its demand. In `ebgp` mode the pairs stay inside one AS.
- **Sessions.** On Kathara, each business prefix hosts a web/push service on its originator's service address (the prefix's `.1`), and the three stubs that send that PoP the most demand keep persistent sessions and page loads to it. Without BGP, and on Containerlab, the busiest PoP's stub hosts the service instead.

In `abilene`, `atlam5` reaches `atlang` over a GRE tunnel whose IP MTU is 1476 on both ends, so full-size TCP across that link relies on Path MTU Discovery.

### Named ISP specials

Complex overlays are fixed scenario IDs (Kathara/FRR only):

| Scenario | Topology | Profile |
| --- | --- | --- |
| `isp_abilene_ebgp_rpki` | Abilene | OSPF + eBGP + offline RPKI/ROV |
| `isp_geant_ebgp_rpki` | GEANT | OSPF + eBGP + offline RPKI/ROV |
| `isp_abilene_ebgp_rtbh` | Abilene | OSPF + eBGP + RTBH blackhole |
| `isp_dfn-bwin_ebgp_rtbh` | DFN-BWIN | OSPF + eBGP + RTBH blackhole |

```shell
uv run nika env run isp_abilene_ebgp_rpki
uv run nika env run isp_abilene_ebgp_rtbh
uv run nika env run isp_dfn-bwin_ebgp_rtbh
```

In the RPKI specials, AS65001 originates `203.0.113.0/24` and `203.0.114.0/24` under matching ROAs. AS65002 also routes both prefixes inside its network to an on-net cache and keeps them out of eBGP with an export-deny prefix list. One AS65003 border router validates received routes against an offline Routinator cache.

NIKA ranks the 26 vendored SNDlib graphs by node count, breaks ties by topology name, and divides the ordered catalog into fixed tiers of 8, 9, and 9 graphs for sampling metadata. Representative graphs: `isp_abilene` (`s`, 12 nodes), `isp_france` (`m`, 25 nodes), `isp_pioro40` (`l`, 40 nodes).

| Topology | Nodes | Links | Demands | Topology | Nodes | Links | Demands |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| `abilene` | 12 | 15 | 132 | `atlanta` | 15 | 22 | 210 |
| `brain` | 161 | 332 | 14,311 | `cost266` | 37 | 57 | 1,332 |
| `dfn-bwin` | 10 | 45 | 90 | `dfn-gwin` | 11 | 47 | 110 |
| `di-yuan` | 11 | 42 | 22 | `france` | 25 | 45 | 300 |
| `geant` | 22 | 36 | 462 | `germany50` | 50 | 88 | 662 |
| `giul39` | 39 | 172 | 1,471 | `india35` | 35 | 80 | 595 |
| `janos-us` | 26 | 84 | 650 | `janos-us-ca` | 39 | 122 | 1,482 |
| `newyork` | 16 | 49 | 240 | `nobel-eu` | 28 | 41 | 378 |
| `nobel-germany` | 17 | 26 | 121 | `nobel-us` | 14 | 21 | 91 |
| `norway` | 27 | 51 | 702 | `pdh` | 11 | 34 | 24 |
| `pioro40` | 40 | 89 | 780 | `polska` | 12 | 18 | 66 |
| `sun` | 27 | 102 | 67 | `ta1` | 24 | 55 | 396 |
| `ta2` | 65 | 108 | 1,869 | `zib54` | 54 | 81 | 1,501 |

To load the backbone with the full demand matrix or a cached dynamic series on top of the business traffic, replay it with iperf after deployment:

```shell
uv run nika traffic run matrix-replay --mode demands --unit K \
  --max-intervals 1 --background
```

SNDlib traffic values retain their source units. `--unit` tells iperf how to interpret the values; NIKA does not treat them as Mbps by default. Cite [SNDlib](https://sndlib.put.poznan.pl/home.action) when publishing results that use these topologies.

## Topology Zoo ISP scenarios

NIKA also vendors 259 graphs from the [Internet Topology Zoo](http://www.topology-zoo.org/) as `isp_<name>` scenarios, for example `isp_geant2012`, `isp_cogentco` or `isp_garr201201`. Zoo Abilene is skipped because `isp_abilene` already comes from SNDlib, and KDL (754 routers) is skipped because it is too large for one host. Run `nika env list` for the full list.

Zoo scenarios use the same compiler, IGP and BGP options, stub hosts and compatible failures as SNDlib ISP scenarios. They differ in four ways:

- They run on Kathara: FRR by default, or native routers with `--device-profile iosxr` or `--device-profile routeros`.
- Topology Zoo has no demand matrix, so `nika traffic run matrix-replay` rejects them.
- They are excluded from benchmark generation. The `s` / `m` / `l` size is metadata only (15 or fewer routers is `s`, 28 or fewer is `m`, more is `l`).
- The importer adapts each graph:
  - It keeps only the largest connected component and drops self-loops, so every router can reach every other router.
  - Router names are ASCII slugs of the zoo labels, at most 19 characters, with `_2`, `_3` suffixes for duplicates.
  - Parallel zoo edges become parallel links. A link-level failure on one member of a parallel bundle can be masked by ECMP over the others.
  - With `--metric-strategy inv_capacity`, 28 zoo graphs have parallel links with unequal metrics. OSPF keeps each member's metric and forwards over the cheapest one. FRR IS-IS costs a neighbor by its cheapest adjacency but can forward over any of them, so with `--igp isis` every member of a parallel bundle gets the bundle's lowest metric and the bundle forwards as ECMP. Use `--igp ospf` when per-member capacity metrics matter.
  - `LinkSpeedRaw` becomes link capacity in Mbit/s, which `--metric-strategy inv_capacity` uses.

```shell
uv run nika env run isp_geant2012 --igp ospf --bgp-mode ebgp
```

Sizes range from 4 to 197 routers (`isp_cogentco`), and every router also gets a stub host, so the largest labs run about 400 containers. Regenerate the vendored files with `uv run python scripts/import_topology_zoo.py`. Cite the Topology Zoo paper (Knight et al., IEEE JSAC 2011) when publishing results that use these topologies.

## Containerlab Clos scenario

### `min3clos`

NIKA ports Containerlab's `min-clos` example into its runtime and verification contracts. One Nokia SR Linux spine connects two SR Linux leaves; one Linux client sits behind each leaf:

```text
client1 -- leaf1 -- spine -- leaf2 -- client2
```

The fabric uses eBGP (leaf AS 65001 and 65002, spine AS 65056). Its SR Linux configuration also enables OSPFv2 and IS-IS on fabric interfaces and the `system0` loopbacks (`30.0.0.1`-`30.0.0.3`, `3000:30::1`-`3000:30::3`). Fabric and client links are dual-stack. NIKA waits for gNMI, applies YAML with `gnmic`, configures both clients, and checks BGP plus IPv4 and IPv6 cross-leaf and loopback reachability. Use this fixed scenario for SR Linux and Containerlab tests that do not need the larger SNDlib compiler.

The clients run [business traffic](#business-traffic): they replicate data sets over HTTP (2 Mbit/s from `client1` to `client2`, 1 Mbit/s back), and `client1` keeps web sessions to a service on `client2`.

## Kubernetes scenarios

Both fixed Kathara scenarios run one k3s server and five workers on `nika/k3s`. This image adds `tcpdump`, `iperf3`, and `python3` to the pinned `rancher/k3s:v1.34.1-k3s1`, so packet capture, `iperf_test`, and `active_tcp_probe` work on k3s nodes. Each k3s device starts with a shell entrypoint that waits for `/var/run/net-ready`, which device startup creates after interfaces and default routes are configured; the entrypoint then `exec`s k3s as PID1 so the control plane does not race Kathara bridge attachment. NIKA exports a session-specific kubeconfig after verification. Kubernetes MCP tools are documented under [MCP servers](../agents/mcp-servers.md#kubernetes-k8s_mcp_server).

If startup reports an exited k3s node, inspect the reported container state and log tail. For inotify exhaustion, follow [Host inotify limits too low](troubleshooting.md#host-inotify-limits-too-low-k3s--xrd).

NIKA prepares every required in-cluster image with `skopeo`, run from the `nika/skopeo` image, before creating the lab, then imports the archives into all six k3s nodes. Image references are fixed by SHA256 digest, including the sample apps and inference simulator. Preparation fetches the original manifest/index and the Linux host platform's complete layers directly from the registry, validates their hashes, and caches them by digest and architecture under `.nika_cache/`. It does not depend on Docker's local image store or `docker save`.

When upgrading from tag-only caches such as `postgres__16.tar`, NIKA removes the old archives for a scenario after validating that scenario's replacement archives. If preparation fails, NIKA keeps the old archives. Other cache files remain available.

A fresh host needs registry access for this preparation and must support the images' platform. A warm, intact cache can prepare workloads without registry access. Missing/corrupt images, unsupported platforms, and unavailable Helm charts fail deployment explicitly. Workloads use `imagePullPolicy: Never`; k3s nodes also disable upstream registry fallback so system pods use the pinned preloaded images. AgentGateway charts and the Helm download are checksum-verified. Locally built `nika/base` and `nika/frr` remain governed by their Dockerfiles and host build prerequisites.

Controller bootstrap has bounded API/component waits. Failed manifest applies, Helm installs, exited k3s containers, and terminal Pod configuration/process errors report the failed stage and abort startup; the session lifecycle removes that deployment's resources. Ordinary Pending/ContainerCreating transitions are allowed until their stage deadline. Watch progress with:

- stderr: `[k8s-cache]` (host preparation + node import) and, for interactive commands, `[env_verify_progress]` / `[env_verify]` (readiness checks)
- session `nika.jsonl` (`env_preload_progress`, `env_verify_progress`)
- controller bootstrap: `docker exec <controller> tail -f /var/log/startup.log` (`[startup]` stages)

During iterative work, `nika env run <scenario> --no-redeploy` skips tearing down an existing lab instance when you only need a new session.

### `k8s_lab`

![k8s_lab topology: two core routers connect the k3s worker pod and an exit pod that leads through two external autonomous systems to the client.](../../assets/images/kathara_k3s_lab_topo.png)

NIKA defines a two-pod FRR fat-tree around the cluster. Pod 1 hosts the k3s nodes; pod 2 provides an exit path to external ASes and a client. BGP unnumbered connects the fabric. MetalLB advertises LoadBalancer addresses through BGP, NGINX provides ingress, and sample `word` and `weather` applications use PostgreSQL and persistent volumes.

The k3s workers connect to `https://201.1.1.2:6443`, matching the controller's advertised API address. This avoids closing bootstrap connections when agents discover the server endpoint. The ingress service uses `externalTrafficPolicy: Local`; its controller pod must run on `worker1` through `worker5`, which have configured MetalLB BGP peers. Required node affinity enforces that placement so MetalLB can advertise the ingress VIP.

`dc_exit` is a plain Linux router with static routes (no routing daemon): it enters the data center through both `leaf_2_*` routers and defaults toward `as1r1`. MetalLB addresses (`101.0.0.0/8`) are served on their service ports only. The k3s workers and the `leaf_2_*` routers discard unused addresses in the `101.0.0.0/8` and `201.0.0.0/8` aggregates, so ICMP to the ingress VIP times out instead of looping while HTTP and HTTPS work.

The client browses both applications through the ingress (`http://datacenter.com/word` and `/weather`) every few seconds, and a per-node `uptime-agent` DaemonSet in the `monitoring` namespace calls them through their Service names.

Use this scenario for faults that combine Kubernetes state with routed underlay behavior. Verification checks the BGP fabric, six Ready nodes, cross-leaf reachability, ingress addressing, and both applications.

### `llmd_lab`

NIKA connects the controller, five workers, and a client to one L2 domain on `200.0.0.0/24`. MetalLB runs in L2 mode. Gateway API resources route requests through an InferencePool and endpoint picker to three prefill pods and two decode pods. `llm-d-inference-sim` models prefill and decode delays without GPUs.

```text
controller  worker1  worker2  worker3  worker4  worker5  client
     \         |        |        |        |        |      /
                 shared L2: 200.0.0.0/24
                            |
              Gateway -> InferencePool -> EPP
                            |
                   prefill and decode pods
```

The client sends chat completions to the gateway (`http://llmd/v1/chat/completions`) every few seconds and repeatedly downloads a dataset from the web server (`http://200.0.0.8/dataset.bin`); a per-node `uptime-agent` DaemonSet in the `monitoring` namespace sends inference requests to the gateway Service.

Use this scenario for Kubernetes service, DNS, policy, and inference-routing faults without a routed fabric. Verification checks node readiness, llm-d and gateway pods, the LoadBalancer address, model discovery, and a simulated chat completion.

## Business traffic

Many faults only hurt traffic that is actually flowing: a NAT that drops live sessions, a QoS policy that demotes voice, a receive window that slows downloads. An environment can therefore declare the traffic its users and services generate at all times. NIKA starts that background business traffic on every session start (`nika env run`, benchmark trials, audits) after lab verification and before any fault is injected, so healthy and faulty tasks carry the same traffic. The programs run inside the lab nodes and stop when the lab is undeployed.

Background business traffic belongs to the environment. Some incidents also start traffic of their own after injection. `tcp_syn_flood_attack` launches SYN generators; `web_dos_attack` and `load_balancer_overload` start ApacheBench workers. Those generators are part of the injected incident, not the environment workload. See the [failure reference](failures.md#registered-failures).

Every environment declares business traffic. The programs run on Linux hosts and servers, so the traffic crosses IOS XR, RouterOS, and SR Linux routers the same way it crosses FRR routers. To add a configurable traffic pattern on top of it, see [Background traffic profiles](#background-traffic-profiles).

To declare traffic for an environment, return a `BusinessWorkload` from the environment's `business_workload()` method ([`nika.traffic.workload`](../../src/nika/traffic/workload.py)):

```python
from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
)

class MyEnv(NetworkEnvBase):
    def business_workload(self) -> BusinessWorkload | None:
        return BusinessWorkload(
            processes=(
                WorkloadProcess("srv", "file_server", "filedist", ("--rate", "5000k")),
                WorkloadProcess("pc1", "file_sync", "filesync", ("http://10.0.0.2:8080/large.bin",)),
            ),
            exercises=(WorkloadExercise(BULK_DOWNLOAD, ("pc1",)),),
        )
```

- `WorkloadProcess(node, app, command, args)` runs the standalone program `app` from [`nika/traffic/apps/`](../../src/nika/traffic/apps/) on `node`, installed as `/usr/local/bin/<command>`. Choose neutral command names that describe the service, since agents see them in `ps`. Each `(node, command)` pair is unique. Starting is idempotent. If any process does not stay up, the session start fails.
- `exercises` records what the traffic guarantees, as an `(kind, target)` pair. A failure whose symptom needs traffic sets `SYMPTOM_WORKLOAD` to a kind and implements `symptom_workload_target(inject)`. Benchmark target generation and inject validation then keep only targets the workload exercises.

| Exercise kind | Target | Guarantee |
| --- | --- | --- |
| `realtime_bulk_contention` | `(router, egress_interface)` | DSCP EF media and best-effort bulk share the egress; bulk stays below the best-effort ceiling, EF media moved into best effort would exceed it |
| `bulk_download` | `(receiver,)` | Repeated TCP bulk transfers into the host |
| `bulk_transit` | `(node, egress_interface)` | At least 1.5 Mbit/s of sustained TCP bulk transfers leave the interface |
| `nat_sessions` | `(nat_router, inside_prefix)` | Persistent and bursty TCP sessions through the router's source NAT, more concurrent connections to one destination than a 64-port pool holds |
| `vip_keepalive_sessions` | `(gateway,)` | Short-lived keep-alive sessions to the stateful L4 VIP, so new connections keep opening through the gateway |
| `fan_in_bursts` | `(receiver,)` | Several workers answer the receiver's query at the same moment, each with a block of back-to-back datagrams, so the answers converge on the receiver's last-hop egress port |

| App | Behavior |
| --- | --- |
| `file_server` | Static HTTP server with a per-connection pacing limit (`--rate`); a request can ask for a lower rate with `?rate=` |
| `file_sync` | Downloads URLs in a loop with a random pause; `--parallel` syncs each URL on its own connection |
| `media_peer` | Sends and receives constant-rate RTP over UDP, marked DSCP EF (`--peer HOST:PORT/KBPS[/BYTES]`); logs per-stream receive loss and RFC 3550 jitter to `--log` |
| `web_service` | HTTP/1.1 service with long-poll, object, and upload routes |
| `web_client` | Persistent long-poll or telemetry sessions, parallel page loads, and uploads |
| `browser` | Users fetching random URLs over HTTP or HTTPS with a random pause, resolving names each time; optional gzip decoding and keep-alive connections |
| `fanout` | Partition/aggregate over UDP: an `aggregator` queries every `worker` at once and each answers with a block of datagrams |

Keep the traffic light enough for size `l` and keep healthy links below saturation, so that ping-based probes do not read the background traffic as a fault.

### Background traffic profiles

Business traffic differs between environments, because each one models its own users. To compare agents under the same kind of load in every environment, or to stress an environment with a known traffic pattern, add a background traffic profile. NIKA starts it right after the business traffic and keeps it running for the whole task. Its endpoints are the hosts and servers that PingMesh probes. On Kubernetes nodes, which have no Python, each program runs in a pod in the `traffic` namespace, pinned to its node with host networking, so it uses the node's own addresses.

Select a profile in a benchmark task with `traffic:`, either as a pattern name or as a mapping:

```yaml
- scenario: dc_clos
  topo_size: s
  problem: link_down
  traffic:
    pattern: gravity
    flow_sizes: hadoop
    load_mbps: 4
  inject: {host_name: leaf_0_0, intf_name: eth1}
```

For an interactive session, pass the same settings to `nika env run`:

```shell
uv run nika env run dc_clos -s s --traffic gravity,flow_sizes=hadoop,load_mbps=4
```

A profile combines a traffic matrix, a flow-size distribution, and Poisson flow arrivals, the way data-center transport and WAN traffic-engineering studies describe their workloads. Each sender is a `flowgen` process that opens TCP flows to `flowsink` processes on the other hosts.

| `pattern` | Traffic matrix | Typical use |
| --- | --- | --- |
| `uniform` | Every permitted host pair gets the same share (pFabric, HPCC, Homa) | Data-center fabrics |
| `gravity` | Each host draws an exponential sending and receiving mass; a pair's share is the product (Roughan, CCR 2005) | WAN and ISP backbones without measured demands |
| `demand` | The environment's measured demand matrix; ISP environments map the SNDlib matrix to their edge hosts | SNDlib ISP backbones |
| `permutation` | Every host sends to exactly one other host (VL2, Hedera) | Bisection and path-diversity load |
| `hotspot` | `hot_fraction` of the load goes to `hotspots` receivers, the rest is uniform | Popular servers in campus and enterprise networks |
| `incast` | `uniform`, plus `fan_in` hosts that answer one receiver's query at the same moment every `incast_period_sec` (DCTCP partition/aggregate) | Shallow-buffer and fan-in congestion |

| Field | Default | Meaning |
| --- | --- | --- |
| `flow_sizes` | `websearch` | Flow-size CDF: `websearch` (DCTCP), `datamining` (VL2), or `hadoop` (Facebook, Roy et al. SIGCOMM 2015) |
| `load_mbps` | `4` | Offered load summed over all senders, in Mbit/s. Lab links are software switches, so keep it well below the link rates of the environment. |
| `max_flow_kb` | `2048` | Flows drawn larger than this are clipped to it, so a single flow does not occupy a few-Mbit/s link for minutes. |
| `max_flow_kbps` | unset | Rate each flow is paced to. Unset sends each flow as fast as TCP allows. |
| `seed` | `1` | Seed of the matrix, the senders' arrivals, and their flow sizes |
| `hotspots`, `hot_fraction` | `1`, `0.5` | `hotspot` receivers and their share of the load |
| `fan_in`, `incast_kb`, `incast_period_sec` | `8`, `32`, `2` | `incast` responders, the KB each returns, and seconds between queries |
| `diurnal_period_sec`, `diurnal_amplitude` | unset, `0.5` | When set, the arrival rate follows a sine of this period and relative amplitude |

Host pairs that the environment isolates by design, such as guest LANs or separate autonomous systems, carry no profile traffic. A `demand` profile fails at session start when the environment has no demand matrix. On environments with RouterOS routers, NIKA lowers the load and incast bursts to 15% of the RouterOS send cap, or of the slower rate an environment shapes its paths to, such as the `enterprise_branch` overlay, and paces each flow to that rate, so a single flow cannot fill the link. It logs the values it ran in the `env_traffic` event. A profile is part of the task identity, so the same fault with and without a profile counts as two tasks, and the benchmark audit runs each task with its profile.

### Failure traffic profiles

Some failures only show in the traffic that crosses them: a link that corrupts or drops part of its frames slows the flows that use it. Such failures declare a profile, and the cases that `nika benchmark generate` writes for them carry it in `traffic:`, so these faults have flows on every host pair to act on. `link_flap`, `link_packet_corruption`, `device_forwarding_packet_corruption`, `flow_rule_loop`, `p4_action_selector_member_misconfig`, and `p4_ecmp_group_member_missing` run with `uniform` traffic. `silent_egress_packet_loss` runs without one: on the bmv2 gateways the profile's load alone drops as many packets as the fault does. For every profile its failures use, generation also writes a healthy case with the same profile, so background traffic alone does not show an agent that a fault exists. The case YAML decides what runs: a case without `traffic:`, including every case of an earlier benchmark release, runs without a profile, and a multi-fault case runs the profile its YAML names.

## Telemetry

Agents can investigate an environment with the evidence operators read from monitoring systems: flow records (NetFlow, IPFIX, or sFlow), device metrics (Prometheus), and syslog. Attach any combination of those kinds when you start a session:

```shell
uv run nika env run dc_clos -s s --telemetry netflow --telemetry prometheus --telemetry syslog
```

The same field belongs on a benchmark YAML row. Save this as `telemetry.yaml` and run it:

```yaml
cases:
  - scenario: dc_clos
    topo_size: s
    problem: link_down
    telemetry: [netflow, prometheus, syslog]
    inject:
      host_name: leaf_router_0_0
      intf_name: eth0
```

```shell
uv run nika benchmark run --config telemetry.yaml
```

A task with `telemetry` has a different task ID from the same environment and incident without it. Telemetry runs on Kathara or Containerlab only.

| Kind | Collector | Agent tool |
| --- | --- | --- |
| `netflow` | `nfcapd` (IPFIX/NetFlow) or `sfcapd` (sFlow) | [`netflow_query`](../agents/mcp-servers.md#netflow-kathara_netflow_mcp_server) |
| `prometheus` | Prometheus | [`prometheus_query`](../agents/mcp-servers.md#prometheus-kathara_prometheus_mcp_server) |
| `syslog` | rsyslogd (UDP 514) | [`syslog_query`](../agents/mcp-servers.md#syslog-kathara_syslog_mcp_server) |

NIKA probes each device at runtime and configures only the exporters that device supports. Devices that lack an exporter stay unconfigured. If no device supports a requested kind, the environment still deploys and that collector stays empty. `telemetry_configured` in the session log lists the versions that were found.

Records stay empty until devices export traffic, counters, or log lines. Flow records become queryable about 20 seconds after the traffic. Metric names and PromQL live with [`prometheus_query`](../agents/mcp-servers.md#prometheus-kathara_prometheus_mcp_server).

| Platform | `netflow` | `prometheus` | `syslog` |
| --- | --- | --- | --- |
| Kathara FRR (`nika/frr`) | IPFIX (`pmacctd`) | node_exporter and BGP gauges | `/dev/log` forwarder |
| Kathara Linux (`nika/base`) | not exported | node_exporter | `/dev/log` forwarder |
| SDN Open vSwitch | OVS NetFlow | OpenFlow port counters | `/dev/log` |
| P4 BMv2 | not exported | pipeline counters | `/dev/log` from `fabric_mgr` |
| Containerlab SR Linux | sFlow on extra ethernet | gNMI interfaces and BGP | UDP 514 |
| Containerlab Linux clients | not exported | not exported | not exported |
| XRd | not exported | `xr_cli` interfaces and BGP | Linux `/dev/log` forwarder |
| RouterOS | traffic-flow v9 | SSH interfaces and BGP | remote logging |

`p4_dc_gateway` also records INT-MX hop reports; query those with [`int_query_telemetry`](../agents/mcp-servers.md#telemetry-kathara_telemetry_mcp_server). The INT collector is not a NIKA exporter.

### Management network

Kathara adds an out-of-band LAN, `telemetry_switch`, and `telemetry_collector`. Each exporting device joins on an extra interface after its scenario interfaces, in VRF `mgmt` (table 4242), so routing protocols never advertise it. node_exporter answers only on that network, and the management interface is left out of Prometheus metrics. vrnetlab maps the extra NIC into RouterOS, so NIKA addresses it there instead of in a Linux VRF and does not add it to OSPF.

Containerlab joins the collector to the lab management network. sFlow cannot use that network, so SR Linux flow records use an extra ethernet port and `ip-vrf` `telemetry`. Prometheus collects SR Linux metrics through gNMI on the lab management network. SR Linux sends syslog to the collector over UDP port 514 on that network; NIKA configures the logging destination through gNMI.

The collector is off the topology: it has no scenario interfaces, no injected failures, and no packet captures. NIKA starts collectors and exporters after the environment is verified.

## Inspect and close a scenario

Each deployment creates a session and stores runtime state under `runtime/`. Use the session commands instead of deleting runtime files or backend objects.

```shell
uv run nika session inspect
uv run nika session close -y
```

See the [failure reference](failures.md) for the faults matched to each scenario and the [CLI reference](cli-reference.md) for all deployment options.
