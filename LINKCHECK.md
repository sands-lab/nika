# Containerlab link-failure re-check

Nineteen of twenty Containerlab link-failure cells pass inject, fault verification, symptom, recover, and environment health. The remaining cell (`isp_dfn-bwin` / SONiC / `link_detach`) restores the kernel interface but does not restore ISIS shortest paths. Each cell is the first matching-platform row in `benchmark/working/pool/<environment>/<failure>.yaml`, driven by `tests.support.failure_e2e.run_failure_e2e` and then `evaluate_scenario`. Deployments ran one at a time under `flock /tmp/nika-heavy-lab.lock`.

`kashef-k8s-w1` was unreachable (`No route to host` for `10.112.180.4`). The matrix ran on this host (`xwangz-vm`) with `docker-sonic-vs:202505-1238524` and `ghcr.io/nokia/srlinux:24.10` already loaded. Disk use on `/` stayed at 35%.

Node view is `ip -br link show <intf>; tc qdisc show dev <intf>; tc filter show dev <intf> egress` on the target during the fault. In every cell the node has no tbf, netem, or egress filter for the fault. `link_down` shows `NO-CARRIER` with admin UP. `link_detach` removes the interface.

| Cell | verify | symptom | recover | healthy | Node view |
| --- | --- | --- | --- | --- | --- |
| min3clos / SR Linux / link_down | pass | pass | pass | pass | eth1 DOWN NO-CARRIER, admin UP; qdisc noqueue; empty egress filter |
| min3clos / SR Linux / link_flap | pass | pass | pass | pass | e1-2 UP; qdisc noqueue; empty egress filter |
| min3clos / SR Linux / link_capacity_bottleneck | pass | pass | pass | pass | eth1 UP; qdisc noqueue (no tbf); empty egress filter |
| min3clos / SR Linux / link_packet_corruption | pass | pass | pass | pass | eth1 UP; qdisc noqueue (no netem); empty egress filter |
| min3clos / SR Linux / link_detach | pass | pass | pass | pass | interface absent |
| min3clos / SONiC / link_down | pass | pass | pass | pass | eth1 DOWN NO-CARRIER, admin UP; qdisc noqueue; empty egress filter |
| min3clos / SONiC / link_flap | pass | pass | pass | pass | eth2 UP; qdisc noqueue; empty egress filter |
| min3clos / SONiC / link_capacity_bottleneck | pass | pass | pass | pass | eth1 UP; qdisc noqueue (no tbf); empty egress filter |
| min3clos / SONiC / link_packet_corruption | pass | pass | pass | pass | eth1 UP; qdisc noqueue (no netem); empty egress filter |
| min3clos / SONiC / link_detach | pass | pass | pass | pass | interface absent |
| isp_dfn-bwin / SR Linux / link_down | pass | pass | pass | pass | e1-1 DOWN NO-CARRIER, admin UP; qdisc noqueue; empty egress filter |
| isp_dfn-bwin / SR Linux / link_flap | pass | pass | pass | pass | e1-1 DOWN NO-CARRIER, admin UP; qdisc noqueue; empty egress filter |
| isp_dfn-bwin / SR Linux / link_capacity_bottleneck | pass | pass | pass | pass | e1-4 UP; qdisc noqueue (no tbf); empty egress filter |
| isp_dfn-bwin / SR Linux / link_packet_corruption | pass | pass | pass | pass | e1-1 UP; qdisc noqueue (no netem); empty egress filter |
| isp_dfn-bwin / SR Linux / link_detach | pass | pass | pass | pass | interface absent |
| isp_dfn-bwin / SONiC / link_down | pass | pass | pass | pass | eth1 DOWN NO-CARRIER, admin UP; qdisc noqueue; empty egress filter |
| isp_dfn-bwin / SONiC / link_flap | pass | pass | pass | pass | eth1 UP; qdisc noqueue; empty egress filter |
| isp_dfn-bwin / SONiC / link_capacity_bottleneck | pass | pass | pass | pass | eth4 UP; qdisc noqueue (no tbf); empty egress filter |
| isp_dfn-bwin / SONiC / link_packet_corruption | pass | pass | pass | pass | eth1 UP; qdisc noqueue (no netem); empty egress filter |
| isp_dfn-bwin / SONiC / link_detach | pass | pass | pass | fail | interface absent |

ISP environments used `--backend containerlab --igp isis --bgp-mode none`. First-row inject targets: min3clos client1:eth1 except flap (leaf1 e1-2 / eth2); ISP berlin e1-1/eth1 except capacity (frankfurt e1-4 / eth4).

## Code changes from failed cells

- End-host addressing probes skip invalid `ip` text, look at data-plane addresses, and fall back to the observer default gateway when SR Linux / SONiC keep IPv4 in the NOS.
- Containerlab capacity E2E hooks read the peer from the environment inventory and check TBF on the host wire, not Kathara's VDE proxy.
- Corruption goodput treats a failed TCP transfer (`received_bps is None`) as a collapse, matching netem that breaks the handshake.
- Flap loss bounds follow the configured down/up duty cycle. A meshed IGP flap that stickies onto a longer detour still counts as verified.
- SONiC `reattach_interface` does not delete CONFIG_DB `PORT` when the wired veth is already back in the namespace. That recreate was replacing it with an unwired SAI port.

## Remaining

`isp_dfn-bwin` / SONiC / `link_detach` restores the kernel interface (`recover` pass) but `evaluate_scenario` still reports `igp_adjacencies` false and `fib_shortest_paths` false: berlin↔frankfurt loopbacks exit via Hamburg (cost 20) instead of the restored eth1 (cost 10). All pairs stay reachable. SR Linux detach on the same link returns to shortest paths.
