# Network scenario reference

This reference helps benchmark operators choose and configure a NIKA network scenario. This checkout registers 15 scenario IDs. Fourteen use one backend; `isp` supports both Kathara and Containerlab.

The [`net_env_pool.py`](../src/nika/net_env/net_env_pool.py) registry defines the authoritative scenario IDs, backends, tags, and size controls. Backend implementations live under [`net_env/`](../src/nika/net_env/). Confirm the installed checkout with:

```shell
uv run nika env list
```

## Install a lab backend

Kathara scenarios need Docker and the Kathara dependency group. Containerlab scenarios need Docker, `clab`, and the Containerlab dependency group.

```shell
# Install both backends
uv sync --extra labs

# Install one backend
uv sync --extra kathara
uv sync --extra containerlab
```

`min3clos` also calls `gnmic` and uses Nokia SR Linux and network-multitool images. `p4_int` needs the local `kathara/influxdb` image described under [P4 scenarios](#p4-scenarios). The Kubernetes scenarios download k3s and workload images during deployment.

## Scenario catalog

| Scenario ID | Backend | Scale control | Options | Network and workload |
| --- | --- | --- | --- | --- |
| `dc_clos` | Kathara | `-s s\|m\|l` | `--workload host\|service` (default `host`) | FRR eBGP Clos; host or DNS/HTTP leaf workload |
| `campus_lan` | Kathara | `-s s\|m\|l` | `--workload static\|dhcp` (default `static`) | Hierarchical campus LAN; static hosts or DHCP/DNS/LB farm |
| `enterprise_branch` | Kathara | `-s s\|m\|l` |  | Hub-and-spoke enterprise WAN: provider underlay + WireGuard + eBGP overlay with per-role VRFs |
| `simple_bgp` | Kathara | Fixed |  | Two FRR ASes with one host in each AS |
| `sdn_star` | Kathara | `-s s\|m\|l` |  | POX and Open vSwitch star |
| `sdn_clos` | Kathara | `-s s\|m\|l` |  | POX and Open vSwitch leaf-spine Clos |
| `p4_bloom_filter` | Kathara | Fixed |  | BMv2 flow-counting Bloom filter pipeline |
| `p4_counter` | Kathara | Fixed |  | BMv2 L2 forwarding and port counters |
| `p4_int` | Kathara | Fixed |  | BMv2 in-band telemetry and collector |
| `p4_mpls` | Kathara | Fixed |  | BMv2 MPLS classification and label switching |
| `isp` | Kathara or Containerlab |  | `--topo` and protocol options | SNDlib topology compiled to FRR or SR Linux |
| `min3clos` | Containerlab | Fixed |  | Five-node SR Linux eBGP Clos |
| `k8s_lab` | Kathara | Fixed |  | FRR fat-tree with a six-node k3s cluster |
| `llmd_lab` | Kathara | Fixed |  | L2 k3s cluster with simulated llm-d inference |

Six scenario IDs accept `s`, `m`, or `l`. Pass a size when you deploy one:

```shell
uv run nika env run dc_clos -s s
```

Fixed scenarios reject a size. The `isp` scenario uses its own topology and protocol controls instead of `-s`.

## Data-center Clos scenario

NIKA builds one Clos fabric from code. Each router runs FRR eBGP. Inter-router links use `/31` networks from `172.16.0.0/16`, and leaf access networks use `10.<pod>.<leaf>.0/24`. Choose the leaf workload with `--workload` (`host` is the default).

```text
                    super-spine(s)
                  /       |       \
             spine(s) ... spine(s)
               /  \           /  \
            leaf  leaf  ...  leaf  leaf
             |      |          |      |
             +------ host or service endpoints
```

| Size | Super-spines and pods | Spines per pod | Leaves per pod |
| --- | ---: | ---: | ---: |
| `s` | 1 | 2 | 2 |
| `m` | 2 | 4 | 4 |
| `l` | 4 | 8 | 8 |

Super-spines use AS 65000, spines use AS 651xx, and leaves use AS 652xx. Workload does not change spine or leaf counts.

### `host` workload (default)

Attaches one `pc_*` host to each leaf. Use this for BGP, link, MTU, congestion, and host addressing failures. The verifier checks FRR, BGP sessions, and host reachability.

```shell
uv run nika env run dc_clos -s s --workload host
```

### `service` workload

Keeps the same fabric and replaces leaf hosts with DNS/HTTP endpoints. Each pod has one authoritative BIND server, HTTP servers on the remaining leaves, and an external client on the super-spine (`192.168.<pod>.0/24`). Clients resolve names such as `web0.pod0`. Use this when the failure needs DNS or HTTP observability.

```shell
uv run nika env run dc_clos -s s --workload service
```

Legacy benchmark YAML may still name `dc_clos_bgp` or `dc_clos_service`. Loaders map those ids to `dc_clos` with `host` or `service` workload. They are not listed by `nika env list`.

## Campus LAN scenario

NIKA builds one campus LAN fabric: a three-router core triangle, distribution and access tiers, user LANs, and a server farm on core3. FRR advertises routed links and services through OSPF. Routed uplinks use `/31` networks from `172.16.0.0/16`; user LANs use `10.<core>.<distribution>.0/24`. Choose the host and farm workload with `--workload` (`static` is the default). Workload does not change core, distribution, or access counts. Device names stay workload-specific so frozen release inject targets remain valid.

```text
user PCs -- access switches -- distribution routers
                                      \       /
                                  core triangle
                                        |
                              server-access device
                               /    |     |    \
                             DNS  HTTP  DHCP*  load balancer*

* Present with --workload dhcp.
```

| Size | Distribution routers | Access switches | User hosts |
| --- | ---: | ---: | ---: |
| `s` | 2 | 2 | 2 |
| `m` | 4 | 8 | 16 |
| `l` | 8 | 32 | 128 |

### `static` workload (default)

Hosts use static addresses and default routes. Dist/access names stay `switch_dist_*` / `switch_server_access`. The farm has one BIND server and four Apache sites (`web0.local` through `web3.local`). Use this for sticky host-address faults such as `host_incorrect_ip`.

```shell
uv run nika env run campus_lan -s s --workload static
```

### `dhcp` workload

Hosts acquire addresses from `dhcp_server`; distribution routers relay DHCP. Dist/access names stay `router_dist_*` / `server_access_router`. The farm adds an NGINX load balancer at `web99.local` and three backend webs on `20.200.0.0/24`. Use this for DHCP, DNS, and load-balancer failures. Both workloads verify OSPF adjacency, cross-branch reachability, DNS, and HTTP.

```shell
uv run nika env run campus_lan -s s --workload dhcp
```

Legacy benchmark YAML may still name `ospf_enterprise_static` or `ospf_enterprise_dhcp`. Loaders map those ids to `campus_lan` with `static` or `dhcp` workload. They are not listed by `nika env list`.

## Enterprise Branch VPN scenario

### `enterprise_branch`

NIKA builds a multi-site enterprise WAN from one production template: HQ and a secondary DC hub, branch sites, and dual provider underlays. Every size keeps the same dual providers, dual hubs, WAN redundancy, and WireGuard+eBGP overlay. Size scales branch count and hosts per LAN; `m` and `l` also add an IOT VRF so complexity grows with business domains, not only replicated VLANs.

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

Use this scenario for underlay vs overlay diagnosis, VRF business isolation, hub-and-spoke VPN reachability, eBGP path preference with backup sessions at every scale, and overlay-egress DSCP/QoS faults. Verification covers VRF devices on every edge, every designed tunnel (underlay reachability, WireGuard, BGP both sides), per-VRF RIB contents (CORP sees CORP+SERVER leak; GUEST/IOT do not), hub interconnect, every branch CORP↔HQ CORP plus HTTP to HQ SERVER, every branch pair via the corp VRF overlay path, every provider without enterprise prefixes, GUEST/IOT isolation from CORP, every backup BGP session with primary-path preference, and HTB EF/BE classes on every WireGuard overlay egress.

Legacy id `rip_small_internet_vpn` (and the short-lived `enterprise_branch_vpn`) resolve to this scenario. They are not listed by `nika env list`. Frozen release `0.1.0` still records the old RIP mini-Internet lab hash and host-VPN selected case; regenerate a release when you publish the new lab.

Boundary: `campus_lan` is a single-campus L3 network; `dc_clos` is a data-center fabric; `isp` is carrier IGP/BGP itself. This scenario is enterprise multi-site WAN with encrypted overlay and per-role VRFs.

```shell
uv run nika env run enterprise_branch -s s
```

## Fixed BGP scenario

### `simple_bgp`

`simple_bgp` is a hand-defined two-AS Kathara lab:

```text
pc1 -- router1 == eBGP == router2 -- pc2
```

`pc1` uses `195.11.14.2/24`; `pc2` uses `200.1.1.2/24`. Each FRR router advertises its host-facing network. Use this small topology for fast BGP and end-to-end routing experiments. Verification checks the BGP session, routes, default gateway, and traffic between the hosts.

## SDN scenarios

NIKA generates both SDN topologies with Open vSwitch data planes and a POX controller at `20.0.0.100:6633`. Hosts use `10.0.0.0/24`.

### `sdn_star`

Each edge switch connects one host to `switch_0`. POX runs `forwarding.l2_learning`.

```text
pc1 -- switch_1 --\
pc2 -- switch_2 ---+-- switch_0
 ...               |
pcN -- switch_N --/

POX controller == control network == all switches
```

| Size | Edge switches and hosts | Total switches |
| --- | ---: | ---: |
| `s` | 4 | 5 |
| `m` | 8 | 9 |
| `l` | 16 | 17 |

### `sdn_clos`

Each leaf connects to every spine and to its host group. POX discovery and spanning tree suppress redundant flooding before the learning-switch module handles traffic.

```text
              spine_1 ... spine_N
                |  \     /  |
                |   \   /   |
              leaf_1 ... leaf_N
               / |         | \
             PC  PC       PC  PC

POX controller == control network == all switches
```

| Size | Spines | Leaves | Hosts per leaf | Total hosts |
| --- | ---: | ---: | ---: | ---: |
| `s` | 1 | 2 | 2 | 4 |
| `m` | 2 | 4 | 4 | 16 |
| `l` | 4 | 8 | 8 | 64 |

Use the star for a single aggregation point and the Clos for redundant paths. Both verifiers check the controller process, OVS readiness, addressing, and host reachability.

## P4 scenarios

The four fixed Kathara scenarios compile repository-owned P4 programs during BMv2 startup and load forwarding entries through `simple_switch_CLI`.

### `p4_bloom_filter`

Two BMv2 switches connect `pc1` and `pc2`. The pipeline hashes TCP five-tuples into two register positions and drops a flow after both counters exceed `PACKET_THRESHOLD` (1000 in the healthy program). Use this scenario for Bloom filter thresholds, compiler failures, and table-entry failures.

```text
pc1 -- switch_1 -- switch_2 -- pc2
         Bloom-filter P4 pipeline
```

### `p4_counter`

Four BMv2 switches connect three hosts over two paths. The program forwards on destination MAC address and records ingress and egress counters per port. Use it for forwarding-table and counter-aware troubleshooting.

```text
              switch_2
             /        \
pc1 -- switch_1        switch_4 -- pc2
             \        /           \
              switch_3             pc3
```

### `p4_int`

Two leaves and two spines connect two hosts and an INT collector. The pipeline adds per-hop telemetry and exports reports for flow statistics, hop latency, port utilization, and queue occupancy. Build the required collector image before deployment:

```text
               spine_1
              /       \
pc1 -- leaf_1           leaf_2 -- pc2
              \       /    \
               spine_2      collector
```

```shell
docker build -t kathara/influxdb src/nika/net_env/kathara/p4/p4_int
```

### `p4_mpls`

Seven BMv2 switches connect three hosts. Border switches classify IPv4 destinations into labels, transit switches forward on labels, and the egress border removes the MPLS header. Labels 2 and 3 carry traffic away from `pc1`; label 1 carries return traffic. Use this scenario for MPLS table and label-limit failures.

```text
pc1 -- switch_1
          | \
          |  +-- switch_2 --\
          |                  +-- switch_4 -- switch_5 --\
          +----- switch_3 --/             \-- switch_6 --+-- switch_7 -- pc2
                                                                        \-- pc3
```

All four verifiers check BMv2 processes, host addresses, and expected end-to-end reachability. `p4_int` also checks its collector.

## SNDlib ISP scenario

### `isp`

NIKA imports SNDlib XML through a backend-neutral topology model. It converts each SNDlib node into a router, each physical link into a point-to-point `/31`, and each router into an attachment point for a `pc_<router>` traffic stub. Kathara renders the plan as FRR routers. Containerlab renders the same plan as Nokia SR Linux routers. SNDlib supplies topology, link attributes, and demand matrices; NIKA supplies the IGP and optional BGP policy presets.

```text
pc_A -- router_A ===== router_B -- pc_B
           \            /
            === router_C === router_D -- pc_D
                  |
                 pc_C

Each ===== link comes from the selected SNDlib graph.
```

```shell
# Default: Kathara, polska, IS-IS, constant metric 10, no BGP
uv run nika env run isp

uv run nika env run isp --topo abilene --igp ospf \
  --metric-strategy routing_cost --bgp-mode ibgp_rr

uv run nika env run isp --backend containerlab \
  --device-profile nokia_srlinux --topo pdh --igp isis
```

| Control | Accepted values | Default | Effect |
| --- | --- | --- | --- |
| `--backend` | `kathara`, `containerlab` | `kathara` for `isp` | Selects FRR or SR Linux rendering |
| `--device-profile` | `frr`, `nokia_srlinux` | Derived from backend | Validates the router profile |
| `--topo` | Catalog name or `network.xml` path | `polska` | Selects the physical graph and demands |
| `--igp` | `isis`, `ospf` | `isis` | Selects the IGP compiler |
| `--metric-strategy` | `constant`, `routing_cost`, `inv_capacity` | `constant` | Maps SNDlib link data to IGP metrics |
| `--constant-metric` | Positive integer | `10` | Sets constant and fallback metrics |
| `--bgp-mode` | `none`, `ibgp_rr`, `ebgp` | `none` | Adds a NIKA-defined BGP preset |

`ibgp_rr` uses AS 65000, selects up to two route reflectors, and originates up to three TEST-NET business prefixes. `ebgp` partitions sorted routers into up to three ASes and creates sessions only on links crossing an AS boundary. It does not add iBGP inside each partition, except for **Abilene + eBGP**, which enables a fixed inter-AS / RPKI profile: intra-AS iBGP meshes, a Routinator RTR with offline SLURM VRPs, a leaker AS with healthy export deny for leak-target prefixes, one ROV observer that rejects RPKI Invalid routes, and one non-ROV observer. That profile supports `bgp_rpki_invalid_route_leak`.

The vendored SNDlib catalog contains 26 topologies:

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

Both fixed Kathara scenarios run one k3s server and five workers on the pinned image `rancher/k3s:v1.34.1-k3s1`. Each k3s device starts with a shell entrypoint that waits for `/var/run/nika-net-ready`, which device startup creates after interfaces and default routes are configured; the entrypoint then `exec`s k3s as PID1 so the control plane does not race Kathara bridge attachment. NIKA exports a session-specific kubeconfig after verification and registers the Kubernetes MCP service for troubleshooting agents.

### `k8s_lab`

![k8s_lab topology: two core routers connect the k3s worker pod and an exit pod that leads through two external autonomous systems to the client.](../assets/images/kathara_k3s_lab_topo.png)

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
