#!/bin/bash
#
set -e
#
if ! command -v wg >/dev/null; then
  echo "wireguard-tools package is not installed (should be installed during initial configuration)" >&2
  exit 1
fi
#
setup_wg_vrf_underlay() {
  local vrf_table="$1"
  local rule_pref="$2"
  local tunnel_af="$3"

  # The global WireGuard listening socket must accept encrypted packets that
  # arrive on a VRF (l3mdev) underlay interface. This is gated by the kernel's
  # udp_l3mdev_accept toggle, which is exposed only as net.ipv4.udp_l3mdev_accept
  # even though it applies to IPv6 too: the UDP l3mdev socket-match helper reads
  # net->ipv4.sysctl_udp_l3mdev_accept regardless of the socket's address family,
  # and there is deliberately no net.ipv6 counterpart. So we set it for both the
  # IPv4 and IPv6 tunnel underlays.
  sysctl -qw net.ipv4.udp_l3mdev_accept=1

  local ip_af=""
  [ "${tunnel_af}" = "ipv6" ] && ip_af="-6"

  if ! ip ${ip_af} rule show pref "${rule_pref}" 2>/dev/null | grep -q "fwmark ${rule_pref}.*lookup ${vrf_table}"; then
    ip ${ip_af} rule add pref "${rule_pref}" fwmark "${rule_pref}" lookup "${vrf_table}"
  fi
}
#
if [ ! -e /sys/class/net/wg_hq ]; then
  ip link add wg_hq type wireguard
fi
# Linux interface parameters for wg_hq
#
sysctl -qw net/ipv6/conf/wg_hq/disable_ipv6=1
ip link set dev wg_hq mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_hq \
  private-key <(printf '%s\n' 'wEzFSwngsTTLOHhpz/SmXaI5LAGSnnc93gHacGzaAk4=') \
  listen-port 51820 \
peer 09xC+mCmgSyePJItfxFJadW+CYzIrQZA/aNelCil3lA= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.1:51820 \
  persistent-keepalive 25

ip link set dev wg_hq up
if [ ! -e /sys/class/net/wg_hq_b ]; then
  ip link add wg_hq_b type wireguard
fi
# Linux interface parameters for wg_hq_b
#
sysctl -qw net/ipv6/conf/wg_hq_b/disable_ipv6=1
ip link set dev wg_hq_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_hq_b \
  private-key <(printf '%s\n' 'wEzFSwngsTTLOHhpz/SmXaI5LAGSnnc93gHacGzaAk4=') \
  listen-port 51821 \
peer 09xC+mCmgSyePJItfxFJadW+CYzIrQZA/aNelCil3lA= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.5:51821 \
  persistent-keepalive 25

ip link set dev wg_hq_b up
if [ ! -e /sys/class/net/wg_br1_b ]; then
  ip link add wg_br1_b type wireguard
fi
# Linux interface parameters for wg_br1_b
#
sysctl -qw net/ipv6/conf/wg_br1_b/disable_ipv6=1
ip link set dev wg_br1_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br1_b \
  private-key <(printf '%s\n' 'wEzFSwngsTTLOHhpz/SmXaI5LAGSnnc93gHacGzaAk4=') \
  listen-port 51822 \
peer 0zs157c4sM3B/6cQO49C+9nrQ+w6IJlYB2K9qchJ52s= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.17:51822 \
  persistent-keepalive 25

ip link set dev wg_br1_b up
if [ ! -e /sys/class/net/wg_br2_b ]; then
  ip link add wg_br2_b type wireguard
fi
# Linux interface parameters for wg_br2_b
#
sysctl -qw net/ipv6/conf/wg_br2_b/disable_ipv6=1
ip link set dev wg_br2_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br2_b \
  private-key <(printf '%s\n' 'wEzFSwngsTTLOHhpz/SmXaI5LAGSnnc93gHacGzaAk4=') \
  listen-port 51823 \
peer 9TZmTy1wikaJp8h0d6E9BYjuWnQA0pwX3s5D7cN7P2E= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.25:51822 \
  persistent-keepalive 25

ip link set dev wg_br2_b up
exit 0