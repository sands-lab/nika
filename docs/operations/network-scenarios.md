# Network scenario reference

This reference helps benchmark operators choose and configure a NIKA network scenario. This checkout lists scenario IDs through `nika env list` (including one ID per SNDlib ISP topology plus named ISP specials). Most scenarios use one backend; ISP topology scenarios support both Kathara and Containerlab.

The [`net_env_pool.py`](../../src/nika/net_env/net_env_pool.py) registry defines the authoritative scenario IDs, backends, tags, and size controls. Backend implementations live under [`net_env/`](../../src/nika/net_env/). Confirm the installed checkout with:

```shell
uv run nika env list
```

## Backend requirements

Kathará scenarios need Docker and the Kathará dependency group. Containerlab scenarios need Docker, `clab`, and the Containerlab dependency group.

Install both backends with `uv sync --extra labs`. Use `--extra kathara` or `--extra containerlab` for one backend. The root [README](../../README.md#-installation) covers the full installation flow.

`min3clos` also calls `gnmic` and uses Nokia SR Linux and the multi-arch `wbitt/network-multitool` image. The Kubernetes scenarios download k3s and workload images during deployment. `iosxr_simple_bgp` needs a manually loaded Cisco XRd Control Plane image; see [IOS-XR simple BGP](#ios-xr-simple-bgp-scenario).

## Scenario catalog

| Scenario ID | Backend | Scale control | Options | Network |
| --- | --- | --- | --- | --- |
| `dc_clos` | Kathara | `-s s\|m\|l` | — | FRR eBGP Clos with DNS/HTTP leaf services |
| `campus_lan` | Kathara | `-s s\|m\|l` | — | Hierarchical campus LAN with DHCP/DNS/LB farm |
| `enterprise_branch` | Kathara | `-s s\|m\|l` |  | Hub-and-spoke enterprise WAN: provider underlay + WireGuard + eBGP overlay with per-role VRFs |
| `sdn_l3_clos` | Kathara | `-s s\|m\|l` |  | ONOS + OVS L3 Clos with SELECT ECMP |
| `p4_dc_fabric` | Kathara | `-s s\|m\|l` |  | BMv2 `simple_switch_grpc` L3 Clos under P4Runtime; ActionSelector ECMP |
| `p4_dc_gateway` | Kathara | `-s s\|m\|l` |  | Gateway-spine-leaf BMv2 fabric with ECMP, INT-MX, ECN, queues, and flow tracking |
| `iosxr_simple_bgp` | Kathara | Fixed |  | Two Cisco XRd routers with eBGP and two PCs |
| `isp_<topology>` | Kathara or Containerlab | Fixed metadata `s`/`m`/`l` | Protocol options | One SNDlib graph per scenario ID, compiled to FRR or SR Linux |
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

Super-spines use AS 65000, spines use AS 651xx, and leaves use AS 652xx.

Each pod has one authoritative BIND server, HTTP servers on the remaining leaves, and an external client on the super-spine (`192.168.<pod>.0/24`). Clients resolve names such as `web0.pod0`. Use this when the failure needs DNS or HTTP observability.

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

Hosts acquire addresses from `dhcp_server`; distribution routers relay DHCP. Dist/access names stay `router_dist_*` / `server_access_router`. The farm adds an NGINX load balancer at `web99.local` and three backend webs on `20.200.0.0/24`. It verifies OSPF adjacency, cross-branch reachability, DNS, and HTTP.

```shell
uv run nika env run campus_lan -s s
```

## Enterprise Branch VPN scenario

### `enterprise_branch`

NIKA builds a multi-site enterprise WAN from one production template: HQ and a secondary DC hub, branch sites, and dual provider underlays. Every size keeps the same dual providers, dual hubs, WAN redundancy, and WireGuard+eBGP overlay. Size scales branch count and hosts per LAN. The `m` and `l` sizes add an IOT VRF, which increases the number of business domains alongside the replicated VLANs.

Each site has business LANs bound into Linux VRFs on the Site Edge (`vrf_corp`, `vrf_server`, `vrf_guest`, and on `m`/`l` `vrf_iot`). Sites do not mesh over physical links. Edges attach to both providers for IP underlay reachability between tunnel endpoints only. WAN PE links, WireGuard tunnels, and eBGP sessions stay in the default VRF.

Site Edge routers terminate WireGuard site-to-site tunnels and run eBGP over those tunnels to exchange authorized business prefixes (CORP, SERVER). FRR imports those overlay prefixes into the matching business VRFs. SERVER is a shared-services domain: CORP and SERVER exchange prefixes through explicit route leaking only. GUEST and IOT stay local (default route via the provider with NAT) and are not advertised in overlay BGP. Cross-site traffic follows `LAN VRF → Site Edge → VPN tunnel → Provider underlay → Remote Edge → Remote LAN VRF`. Branch-to-branch traffic hairpins a hub. HQ and DC2 also peer directly over dual-provider WireGuard+eBGP.

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

Healthy Site Edges preserve DSCP on CORP overlay traffic and shape each WireGuard overlay egress with a dual-class HTB queue: EF (DSCP 46) gets a reserved high-priority class with a deep FIFO; CS0/BE is hard-capped with a deep byte-FIFO so competing bulk builds ~500ms standing delay. Per-size overlay egress capacity is `s` 8 mbit (EF 2), `m` 16 mbit (EF 3), `l` 32 mbit (EF 4). The lab does not start resident CORP QoS traffic; benchmark failures that need competition start ephemeral workloads during inject/verify.

### Underlay WAN propagation delay

Each Site Edge↔provider underlay attachment applies one-way `netem delay 20ms` on both PE ends. A branch→HQ path therefore sees about 40 ms one-way and about 80 ms RTT (plus WireGuard), with no added loss or jitter. Values come from the [Cisco Catalyst SD-WAN Small Branch Design Case Study](https://www.cisco.com/c/en/us/td/docs/solutions/CVD/SDWAN/cisco-sdwan-casestudy-smallbranch.html) (American GasCo):

| Case-study evidence | Value | Lab use |
| --- | --- | --- |
| Table 22 Bulk-Data SLA | 300 ms RTT ceiling | Healthy lab RTT stays well below |
| Table 23 `SLA_BUSINESS_DATA` | 400 ms RTT, 2% loss, 100 ms jitter | Business-data AAR ceiling; BFD metrics are RTT |
| Table 22 Transactional-Data SLA | 50 ms RTT | Tighter interactive apps; bulk paths may exceed this |
| Table 6 Type 1–2 site bandwidth | up to 50 / 150 Mbps | Matches overlay `s`/`m`/`l` = 8 / 16 / 32 mbit |
| Geography | Atlanta HQ, southeastern US stores | Regional branch–DC path |

This delay is scenario fidelity for WAN BDP. It is not part of any failure root cause.

SERVER-role hosts serve static HTTP objects at `http://<server>/small.bin` (16 KB) and `http://<server>/large.bin` (32 MB) for bulk-transfer probes. Host containers run privileged so TCP receive-buffer sysctls (`net.ipv4.tcp_rmem`, `tcp_moderate_rcvbuf`) are writable for receiver-window faults. Docker typically keeps `net.core.rmem_max` read-only; `tcp_rmem` max remains the effective TCP ceiling when it is below the host `rmem_max`.

Use this scenario for underlay vs overlay diagnosis, VRF business isolation, hub-and-spoke VPN reachability, eBGP path preference with backup sessions at every scale, overlay-egress DSCP/QoS faults, and receiver-side TCP receive-window bottlenecks on branch–HQ paths. Verification covers VRF devices on every edge, every designed tunnel (underlay reachability, WireGuard, BGP both sides), per-VRF RIB contents (CORP sees CORP+SERVER leak; GUEST/IOT do not), hub interconnect, every branch CORP↔HQ CORP plus HTTP to HQ SERVER, every branch pair via the corp VRF overlay path, every provider without enterprise prefixes, GUEST/IOT isolation from CORP, every backup BGP session with primary-path preference, and HTB EF/BE classes on every WireGuard overlay egress.

Legacy id `rip_small_internet_vpn` (and the short-lived `enterprise_branch_vpn`) resolve to this scenario. They are not listed by `nika env list`. Frozen release `0.1.0` still records the old RIP mini-Internet lab hash and host-VPN selected case; regenerate a release when you publish the new lab.

Boundary: `campus_lan` is a single-campus L3 network; `dc_clos` is a data-center fabric; `isp_*` scenarios are carrier IGP/BGP itself. This scenario is enterprise multi-site WAN with encrypted overlay and per-role VRFs.

```shell
uv run nika env run enterprise_branch -s s
```

## SDN scenarios

### `sdn_l3_clos`

Symmetric leaf-spine L3 Clos under centralized ONOS control. Switches are Open vSwitch (`fail-mode=secure`, OpenFlow 1.3). The out-of-band control network is `172.31.0.0/16` with ONOS at `172.31.0.100:6653`. On deploy, `ensure_nika_docker_images` builds `nika/onos` when missing and pulls `kathara/sdn` when missing. `nika/onos` wraps pinned `onosproject/onos:2.7-latest` (plus iproute2) and is always built for `linux/amd64` because the upstream image is amd64-only. Apple Silicon runs that image through Docker Desktop/Rosetta. Linux arm64 needs qemu-x86_64 binfmt; otherwise ensure fails early instead of raising `exec format error`. Each leaf owns rack prefix `10.0.<leaf>.0/24` with gateway `.1` and a shared virtual router MAC. Endpoints start at `.11` (one `web_*` nginx endpoint plus `client_*` workers per leaf). A host-side fabric manager installs proactive IPv4 forwarding and OpenFlow `SELECT` ECMP groups (stable five-tuple hash). No STP and no `NORMAL` learning fallback.

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

Symmetric leaf-spine L3 Clos of BMv2 `simple_switch_grpc` switches under a NIKA P4Runtime fabric manager. The out-of-band control network is `172.31.0.0/16` with `fabric_mgr` at `172.31.0.101`. Switches start with `--no-p4` and listen on `:9559`. Deploy compiles the v1model IPv4 fabric program once on a leaf, then `SetForwardingPipelineConfig` plus table/group writes program every switch. Role is table state: same-rack `/32` to the host port, remote `/24` via ActionSelector ECMP over all spines. Endpoints use `/32` addresses on rack `10.0.<leaf>.0/24`, send all IPv4 via gateway `.1`, and keep a permanent neighbor for the shared virtual router MAC. Build `nika/fabric-controller` on deploy when missing (the image name must not contain `p4`; NIKA classifies `"p4" in image` as BMv2).

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

Legacy id `p4_counter` resolves to this scenario. It is not listed by `nika env list`. Frozen release `0.1.0` still records the old L2 counter lab hash; regenerate a release when you publish the new lab.

### `p4_dc_gateway`

This benchmark scenario connects one external client to each gateway, fully meshes gateways to spines and spines to leaves, and connects two HTTP services to each leaf. Every switch also joins a telemetry LAN, and an isolated OOB network carries P4Runtime traffic.

| Size | Gateways | Spines | Leaves | External clients | HTTP services |
| --- | ---: | ---: | ---: | ---: | ---: |
| `s` | 2 | 2 | 2 | 2 | 4 |
| `m` | 4 | 4 | 4 | 4 | 8 |
| `l` | 8 | 8 | 8 | 8 | 16 |

The shared v1model pipeline provides IPv4 LPM, five-tuple ActionSelector ECMP, packet and byte counters, fixed INT-MX source and sink processing, four-position SYN and non-SYN counting Bloom filters, per-port ECN thresholds, queue occupancy, and private post-counter failure hooks. BMv2 and INT MCP tools for this lab are documented under [MCP servers](../agents/mcp-servers.md#p4--bmv2-kathara_bmv2_mcp_server) and [Telemetry](../agents/mcp-servers.md#telemetry-kathara_telemetry_mcp_server).

```shell
uv run nika env run p4_dc_gateway -s s
uv run nika traffic run burst --sources client_1,client_2 --destination service_1_1 --protocol tcp --rate 10M --packet-size 1200 --duration 10 --seed 42
```

## IOS-XR simple BGP scenario

### `iosxr_simple_bgp`

Two Cisco XRd Control Plane routers peer over eBGP, each with one Linux PC. Cisco licensing blocks redistributing or auto-building the image like `nika/*`, so you load and tag it before deploy.

1. Download the XRd Control Plane container tarball from Cisco (CCO account with an XRd Control Plane entitlement, for example through Cisco Software Download or Cisco Modeling Labs). The file looks like `xrd-control-plane-container-x86_64-<version>.tgz`.

2. Load and tag it to the image reference in [`lab.py`](../../src/nika/net_env/kathara/interdomain_routing/iosxr_simple_bgp/lab.py) (`IMAGE`, currently `ios-xr/xrd-control-plane:26.2.1`). For a different XRd version, retag as `26.2.1` or change that constant:

```shell
docker load -i xrd-control-plane-container-x86_64-<version>.tgz
docker tag <loaded-repo>:<loaded-tag> ios-xr/xrd-control-plane:26.2.1
docker images | grep xrd-control-plane
```

If the tag is missing, `nika env run iosxr_simple_bgp` raises a `RuntimeError` with the same `docker load` / `docker tag` steps instead of deploying a broken lab.

3. Raise host inotify limits (XRd Control Plane, IOS XR >= 7.9.2). See the [XRd host setup tutorial](https://xrdocs.io/virtual-routing/tutorials/2022-08-22-setting-up-host-environment-to-run-xrd):

```shell
sysctl -w fs.inotify.max_user_instances=64000
sysctl -w fs.inotify.max_user_watches=64000
```

4. Deploy:

```shell
uv run nika env run iosxr_simple_bgp
```

Each router runs privileged with IPv6 enabled (Kathara device metadata in `lab.py`). XRd ZTP can briefly race the container network namespace at first boot; the router startup scripts retry config apply until that clears, so a slow first boot is expected.

## SNDlib ISP scenarios

### `isp_<topology>`

NIKA imports SNDlib XML through a backend-neutral topology model. It converts each SNDlib node into a router, each physical link into a point-to-point `/31`, and each router into an attachment point for a `pc_<router>` traffic stub. Kathara renders the plan as FRR routers. Containerlab renders the same plan as Nokia SR Linux routers. SNDlib supplies topology, link attributes, and demand matrices; NIKA supplies the IGP and optional BGP policy presets.

Each vendored SNDlib graph is a separate scenario ID (`isp_abilene`, `isp_france`, …). Topology identity is the scenario name. Relative size `s` / `m` / `l` is fixed metadata for benchmark sampling (by node-count tier), not a CLI flag.

```text
pc_A -- router_A ===== router_B -- pc_B
           \            /
            === router_C === router_D -- pc_D
                  |
                 pc_C

Each ===== link comes from the selected SNDlib graph.
```

```shell
# Default: Kathara, IS-IS, constant metric 10, no BGP
uv run nika env run isp_abilene

uv run nika env run isp_abilene --igp ospf \
  --metric-strategy routing_cost --bgp-mode ibgp_rr

uv run nika env run isp_france --backend containerlab \
  --device-profile nokia_srlinux --igp isis
```

| Control | Accepted values | Default | Effect |
| --- | --- | --- | --- |
| `--backend` | `kathara`, `containerlab` | `kathara` | Selects FRR or SR Linux rendering |
| `--device-profile` | `frr`, `nokia_srlinux` | Derived from backend | Validates the router profile |
| `--igp` | `isis`, `ospf` | `isis` | Selects the IGP compiler |
| `--metric-strategy` | `constant`, `routing_cost`, `inv_capacity` | `constant` | Maps SNDlib link data to IGP metrics |
| `--constant-metric` | Positive integer | `10` | Sets constant and fallback metrics |
| `--bgp-mode` | `none`, `ibgp_rr`, `ebgp` | `none` | Adds a NIKA-defined BGP preset |

`ibgp_rr` uses AS 65000, selects up to two route reflectors, and originates up to three TEST-NET business prefixes. `ebgp` partitions the physical graph into up to three connected AS regions. Each AS uses one route reflector, and each physical AS boundary carries an eBGP session. The IGP forms adjacencies inside each AS and treats inter-AS interfaces as passive.

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

Traffic does not start with the lab. Replay the static demand matrix or a cached dynamic series after deployment:

```shell
uv run nika traffic run sndlib --mode demands --unit K \
  --max-intervals 1 --background
```

SNDlib traffic values retain their source units. `--unit` tells iperf how to interpret the values; NIKA does not treat them as Mbps by default. Cite [SNDlib](https://sndlib.put.poznan.pl/home.action) when publishing results that use these topologies.

## Containerlab Clos scenario

### `min3clos`

NIKA ports Containerlab's `min-clos` example into its runtime and verification contracts. One Nokia SR Linux spine connects two SR Linux leaves; one Linux client sits behind each leaf:

```text
client1 -- leaf1 -- spine -- leaf2 -- client2
```

The fabric uses eBGP (leaf AS 65001 and 65002, spine AS 65056). Its SR Linux configuration also enables OSPFv2 and IS-IS on fabric interfaces. NIKA waits for gNMI, applies YAML with `gnmic`, configures both clients, and checks BGP plus cross-leaf traffic. Use this fixed scenario for SR Linux and Containerlab tests that do not need the larger SNDlib compiler.

## Kubernetes scenarios

Both fixed Kathara scenarios run one k3s server and five workers on the pinned image `rancher/k3s:v1.34.1-k3s1`. Each k3s device starts with a shell entrypoint that waits for `/var/run/nika-net-ready`, which device startup creates after interfaces and default routes are configured; the entrypoint then `exec`s k3s as PID1 so the control plane does not race Kathara bridge attachment. NIKA exports a session-specific kubeconfig after verification. Kubernetes MCP tools are documented under [MCP servers](../agents/mcp-servers.md#kubernetes-k8s_mcp_server).

First deployment pulls k3s and in-cluster workload images from the network. Host Docker images are reused automatically when already present. To warm workload image tars and llmd Helm charts before starting a lab:

```shell
uv run nika env cache llmd_lab
uv run nika env cache k8s_lab
uv run nika env cache --all
```

Cached artifacts live under `.nika_cache/` (gitignored). On redeploy, NIKA sideloads cached workload images into k3s nodes instead of pulling from the internet again. During iterative work, `nika env run <scenario> --no-redeploy` skips tearing down an existing lab instance when you only need a new session.

### `k8s_lab`

![k8s_lab topology: two core routers connect the k3s worker pod and an exit pod that leads through two external autonomous systems to the client.](../../assets/images/kathara_k3s_lab_topo.png)

NIKA defines a two-pod FRR fat-tree around the cluster. Pod 1 hosts the k3s nodes; pod 2 provides an exit path to external ASes and a client. BGP unnumbered connects the fabric. MetalLB advertises LoadBalancer addresses through BGP, NGINX provides ingress, and sample `word` and `weather` applications use PostgreSQL and persistent volumes.

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

Use this scenario for Kubernetes service, DNS, policy, and inference-routing faults without a routed fabric. Verification checks node readiness, llm-d and gateway pods, the LoadBalancer address, model discovery, and a simulated chat completion.

## Inspect and close a scenario

Each deployment creates a session and stores runtime state under `runtime/`. Use the session commands instead of deleting runtime files or backend objects.

```shell
uv run nika session inspect
uv run nika session close -y
```

See the [failure reference](failures.md) for the faults matched to each scenario and the [CLI reference](cli-reference.md) for all deployment options.
