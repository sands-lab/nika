# Fault Routing and Tool Index

Maps symptom classes to owning fault-family skills, and diagnostic needs to helper scripts. The phase gates themselves are defined in the system prompt; this index complements them rather than replacing them.

## Phase routing
- Phase 1 (blind start) and Phase 2 (branch) are defined in the system prompt.
- **Symptom present** → select a fault family from the Fault Index and enter its skill only after the family is implicated by a real symptom.
- **No symptom after Phase 1** → enter `diagnosis-methodology-skill` first. Return to this index only after broad search surfaces a symptom.
- `baseline-behavior-skill` is consulted before promoting any observation to a symptom; it does not define a troubleshooting path.
- Entering a family skill is a commitment that the family is implicated. Do not enter a skill speculatively.

## Fault Index

Each row maps a symptom class to a single owning family. When a symptom plausibly fits two rows, the more specific family owns it; see the disambiguation notes below.

| Symptom pattern | Owning family |
|---|---|
| Interface currently DOWN, missing, or detached; active flap script/process; repeated carrier transitions with network-down history | `link-fault-skill` |
| Packet-length / MTU filter rule observed on a host (`link_fragmentation_disabled` is an iptables length rule) | `acl-skill` |
| Quiet throughput drop, bandwidth limit, packet corruption, non-default qdisc stack | `tc-fault-skill` |
| Per-host addressing or identity anomaly on one host (or a small minority): IP, gateway, netmask, missing IP, malformed resolv.conf, static `arp -s`. If every host in a subnet shares the symptom, treat as upstream cascade. | `host-ip-skill` |
| DHCP server-side fault — daemon dead, missing subnet declaration, server pushing wrong gateway/DNS. Requires direct evidence on the DHCP server; hosts without an IP is not sufficient on its own (possible cascade). | `dhcp-fault-skill` |
| Concrete duplicate `link/ether`, or an ARP entry that flips between two MACs for one IP | `mac-address-conflict-skill` |
| Selective protocol or service drop via nft/iptables (HTTP, DNS port, ICMP, BGP, ARP) | `acl-skill` |
| DNS server-side fault — wrong zone answer, named down, port 53 filtered, or a tc delay on the DNS server's egress (`dns_lookup_latency`) | `dns-fault-skill` |
| OSPF or FRR fault — missing neighbor, area mismatch, FRR daemon down, OSPF-protocol ACL | `ospf-fault-skill` |
| BGP advertisement, leak, hijack, ASN, or route-propagation symptom | `bgp-fault-skill` |
| Load balancer on the hot path and itself implicated (VIP slow or timing out, LB command timeout, failed LB IP lookup, LB CPU/socket spike) | `load-balancer-skill` |
| A same-role peer uniquely slow, overloaded, CPU-hot, application-delayed, or timing out as an isolated MCP shell failure | `resource-contention-skill` |
| Device completely silent: ICMP to its known IP fails, shell never opens, AND same-role peers stay clean. Any partial response (ICMP, HTTP, localhost) rules this out and routes to `load-balancer-skill` or `resource-contention-skill` instead. | `host-crash-skill` |
| Oversized tool output exceeding the inline token budget | `big-return-skill` |

### Disambiguation notes
- **Subnet-wide versus per-host scope.** `host-ip-skill` covers one host (or a small minority) whose configuration differs from its same-subnet peers. If the same symptom affects every host in a subnet — or every host overall — it is the leaf of an upstream cascade (routing, ACL, link, FRR/control plane). Run infrastructure and protocol-specific routing checks through MCP before entering `host-ip-skill` or a DHCP family.
- `host_incorrect_dns` (host's own resolv.conf is wrong) belongs to `host-ip-skill`, not `dns-fault-skill`. `dns-fault-skill` covers server-side faults only.
- **DHCP cascade trap.** Hosts without an IP may indicate a genuine DHCP fault or an upstream cascade (broken routing/control plane, ACL, controller, or link making the relay path unreachable). `dhcp-fault-skill` therefore requires direct evidence on the DHCP server — daemon dead via `pgrep` or wrong configuration — and a confirmed path from each affected access gateway or relay to the server before submission.
- `ospf_acl_block` belongs to `ospf-fault-skill`: the symptom is a broken OSPF adjacency; the ACL is the mechanism. Generic ACL drops on other protocols belong to `acl-skill`.
- `link_fragmentation_disabled` is implemented as an iptables length filter and therefore surfaces as an ACL rule. It belongs to `acl-skill` despite its name.
- Transient DHCP no-IP or short lease without direct link evidence stays in `host-ip-skill`; do not reroute to `link-fault-skill`.
- `carrier_changes` alone is weak history. If the host access interface is UP/present and there is no flap script/process or `Network is down` history, keep a one-host missing-IP symptom in `host-ip-skill`.
- Slow service response with LB VIP slow and an LB resource spike belongs to `load-balancer-skill`. A single backend slow while the LB is idle belongs to `resource-contention-skill`.
- **Crash versus contention.** `host_crash` means the device's container was killed, stopped, or removed. If MCP shell probes mark a running web/app server as an isolated timeout/`exec_failed`, route to `resource-contention-skill`; known-IP ICMP success further rules against `host_crash`.
- **Load-balancer guard.** If a device named or classified as `load_balancer` is flagged by reachability, interface, or resource checks through MCP, enter `load-balancer-skill` before considering `host_crash`, `web_dos_attack`, `receiver_resource_contention`, or `sender_resource_contention`. A shell timeout on the LB is direct LB pressure evidence, not proof of `host_crash`, when ICMP or HTTP still partially works.
- **Ping-pair / unknown-status caveat.** `ping_pair(src, dst_name)` and `get_reachability` resolve `dst_name` before pinging. Any name-resolution failure on the source — broken or wrong resolver config, a corrupted/missing DNS record, a crashed or unreachable name server, a transient lookup parse error — produces a row with `status="unknown"` and null tx/rx/loss that is indistinguishable from a path failure. Treat these rows as **unconfirmed** until you re-test with a direct-IP probe: `exec_shell(src, "ping -c2 <dst-ip>")` using the IP listed in the same `get_reachability` `hosts` map. If direct-IP succeeds, the symptom lives in the resolver/DNS/host-identity layer, not in routing or link. This rule is topology- and family-agnostic and must run before entering any fault-family skill driven by an `unknown` row.

## Diagnosis report

Return the affected device or path, observed symptoms, and evidence for the root cause. The host freezes the report and translates it into the scored submission in a separate phase.

## Tool Index

Use `diagnosis-methodology-skill` for ordered infrastructure, routing, host-path, and service checks through MCP. Use `baseline-behavior-skill` to confirm symptoms and `big-return-skill` to parse oversized tool output.
