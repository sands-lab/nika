---
name: link-fault-skill
description: Identify link_flap, link_down, and link_detach faults. Use when an interface is DOWN, missing, detached, repeatedly flapping, or shows network-down history.
---

# Link Faults

## What These Faults Mean

- `link_detach`: the expected interface is missing entirely from the device.
- `link_down`: the interface exists but is down and there is no evidence it is intentionally flapping.
- `link_flap`: the interface repeatedly transitions down and up, so the device may look healthy by the time it is inspected.

## Leading Signals

- Interface state (`ip link show`) proves whether the interface exists and whether it is up right now.
- Direct evidence of repeated link transitions — climbing carrier-changes, kernel link events, or any agent on the host repeatedly toggling the interface — is stronger than transient DHCP fallout alone.
- Link history (startup and DHCP logs) is important when the interface has already recovered by the time you inspect it.

## Exact Calls

- `get_host_net_config(host_name="<host>")`
- `exec_shell(host_name="<host>", command="ip link show")`
- Check for repeated link transitions: interface counters (`ip -s link show <iface>`), kernel link events (`dmesg | grep -i link`), or any process or scheduled job repeatedly toggling the interface.
- `exec_shell(host_name="<host>", command="tail -100 /var/log/startup.log")`

## Submit on match

When direct evidence (current interface state, repeated link transitions, or startup-log link history) is sufficient to identify the fault, submit. Do not chain extra probes once the family is confirmed.

## Recovered Interface Rule

- If the interface is already back UP when you inspect it, history can keep the link family alive — but only with direct evidence of past instability, not inference from downstream symptoms.
- Submit `link_flap` only with direct evidence of repeated transitions (climbing carrier-changes over a short window, kernel link events, or an active agent toggling the interface). A single non-zero `carrier_changes` value on its own is not enough — small counts can be normal startup history.
- If the interface recovered but there is no evidence of repeated flap, prefer `link_down` only when startup or DHCP history contains direct link errors (such as `Network is down`), or when the interface is observed DOWN on one probe and UP on a later probe.
- If the host access interface is UP but the host briefly lost IP or default route, check link history once before leaving the link family.

## Cross-Family Guardrails

- If the host access interface is present and UP but the host lacks IP or default route, do not switch immediately. First rule out an active or recently-recovered link fault using interface counters or kernel link history. If the link is steady and the host's missing-IP / wrong-config state is distinctive (same-subnet peers don't share it), the case belongs to `host-ip-skill`.
- Do not let one no-route snapshot outrank direct evidence of repeated link transitions.
- A later recovered DHCP lease does not by itself reclassify a host-side missing-IP fault as a link fault.
- Never submit `link_down` without first checking whether the interface is flapping rather than statically down.
