---
name: resource-contention-skill
description: Diagnose endpoint CPU contention and abusive HTTP traffic when transfers slow down.
---

# Endpoint resource contention and HTTP DoS

Compare the affected endpoint with a healthy peer. Use `exec_shell` to inspect running processes, socket state, and cgroup CPU allowance and throttling counters. Installed tools alone do not establish contention.

`endpoint_cpu_contention` requires live competing workers and CPU pressure on the endpoint that serves or receives the slow transfer. A command timeout alone does not establish the cause. Confirm that small requests and the network path remain usable.

For `web_dos_attack`, identify the client generating abusive requests or slow connections and the service it targets. The attacking client is the root-cause device; the overloaded server is the victim. Separate that evidence from a local batch job consuming the endpoint's own CPU.

Compare CPU evidence with TCP socket and transfer observations before choosing between CPU contention and `tcp_receive_window_limited`. If the symptom centers on a VIP, use `load-balancer-skill` to compare direct backend requests.
