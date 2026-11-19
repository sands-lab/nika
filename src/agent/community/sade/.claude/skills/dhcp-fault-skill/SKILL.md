---
name: dhcp-fault-skill
description: Identify DHCP server-side faults — service down, missing subnet declaration, incorrect gateway, DNS, or subnet-mask options on the legitimate server. Use when one or more hosts cannot obtain a lease, or hosts received DHCP-supplied values that look wrong.
---

# DHCP Server-Side Faults

## Direct-evidence requirement

The agent must show direct evidence on the DHCP server before submitting any DHCP fault. Hosts having no IP is downstream — it does NOT prove the DHCP server is the cause.

## Rule out upstream cascade first

A broken path to the DHCP server looks identical to a server-side fault from the affected host's perspective. Before submitting any DHCP fault, confirm the upstream isn't the real cause: the routing / control plane between the affected host and the DHCP server must be healthy, no ACL on the relay path may be dropping lease traffic, and the server itself must be reachable from the host's access gateway or relay. If any of those is broken, the upstream wins.

A host having no IP, an expired lease, or a short `valid_lft` is NOT direct evidence of a DHCP fault by itself — only a dead daemon or a wrong server configuration is.

## Submit on match

When direct evidence on the DHCP server is sufficient AND the upstream cascade has been ruled out, submit. Do not chain extra probes once the family is confirmed.

## Disambiguation

- **DHCP option vs host-local misconfiguration.** If the host's wrong gateway/DNS matches what the DHCP server is pushing, this points to a DHCP server fault; the affected host is a symptom. If the DHCP server's config is correct but the host's `/etc/resolv.conf` or default route differs from what was pushed, that's `host-ip-skill` territory.
- **dhcp_service_down vs no-route-to-DHCP.** A host without IP plus a DHCP server whose process is alive = upstream cascade, not `dhcp_service_down`. The cascade guard above must pass first.
- **`systemctl inactive` is not enough.** `systemctl is-active` may report inactive for services that are actually running under a different init pattern. Combine with `pgrep` (or equivalent process check) to confirm the daemon is actually absent before naming a service-down fault.
- **`dhcp_missing_subnet` vs `dhcp_option_subnet`.** A missing subnet declaration prevents lease allocation. An incorrect subnet-mask option changes the mask in an existing declaration and gives clients the wrong prefix length.
