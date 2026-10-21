---
name: diagnosis-methodology-skill
description: Investigate from link state through services when initial reachability checks show no symptom.
---

# Diagnosis Methodology

Enter when initial reachability checks show no symptom. Use the session's MCP tools for network access. Stop broad search when evidence identifies an affected device, path, or service, then enter its fault-family skill.

## Phase A: Links and infrastructure

Use `exec_shell(host_name="<device>", command="...")` on devices from the task description and MCP inventory. Check `ip -j addr`, `ip -s link`, `ip route`, `ip neigh`, `nft list ruleset`, and `cat /etc/resolv.conf`. Compare MAC addresses within each Layer 2 domain, including servers and load balancers. Compare a suspect device with a healthy peer before promoting a difference to a symptom.

## Phase B: Routing and control plane

Use the available routing MCP tools, or `exec_shell` with `vtysh -c 'show ip route'`. For OSPF, check neighbors, interface areas, and FRR processes. For BGP, check peer state, local ASN, advertised prefixes, and blackhole routes. Use controller or switch tools for SDN and P4 environments. Check `tc -s qdisc show` when loss, corruption, latency, or throughput suggests traffic shaping.

## Phase C: Host path

For an implicated host, inspect `ip route get <target-ip>` and the next-hop neighbor entry. Use interface counters, DHCP state, and available logs to check transient link or address loss. If a name-based probe fails, repeat it with a known destination IP before blaming the network path.

## Phase D: Services and resource pressure

Compare DNS responses and HTTP behavior from affected and healthy clients. Check service processes, listening sockets, CPU usage, and running stress or flood processes on the owner. Compare a load balancer's VIP with its backends to locate pressure.

## Finish

Consult `baseline-behavior-skill` before treating an observation as a symptom. Report no anomaly only after examining infrastructure, routing, and services. Return a diagnosis report with the observed evidence; The host freezes it and handles submission.
