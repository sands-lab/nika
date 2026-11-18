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
if [ ! -e /sys/class/net/wg_dc2 ]; then
  ip link add wg_dc2 type wireguard
fi
# Linux interface parameters for wg_dc2
#
sysctl -qw net/ipv6/conf/wg_dc2/disable_ipv6=1
ip link set dev wg_dc2 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_dc2 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51820 \
peer 89Mi8Tkjc30RDxO+mQ4ebJW00oHxEZUpID1ABduDr24= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.9:51820 \
  persistent-keepalive 25

ip link set dev wg_dc2 up
if [ ! -e /sys/class/net/wg_dc2_b ]; then
  ip link add wg_dc2_b type wireguard
fi
# Linux interface parameters for wg_dc2_b
#
sysctl -qw net/ipv6/conf/wg_dc2_b/disable_ipv6=1
ip link set dev wg_dc2_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_dc2_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51821 \
peer 89Mi8Tkjc30RDxO+mQ4ebJW00oHxEZUpID1ABduDr24= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.13:51821 \
  persistent-keepalive 25

ip link set dev wg_dc2_b up
if [ ! -e /sys/class/net/wg_br1 ]; then
  ip link add wg_br1 type wireguard
fi
# Linux interface parameters for wg_br1
#
sysctl -qw net/ipv6/conf/wg_br1/disable_ipv6=1
ip link set dev wg_br1 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br1 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51822 \
peer 0zs157c4sM3B/6cQO49C+9nrQ+w6IJlYB2K9qchJ52s= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.17:51820 \
  persistent-keepalive 25

ip link set dev wg_br1 up
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
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51823 \
peer 0zs157c4sM3B/6cQO49C+9nrQ+w6IJlYB2K9qchJ52s= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.21:51821 \
  persistent-keepalive 25

ip link set dev wg_br1_b up
if [ ! -e /sys/class/net/wg_br2 ]; then
  ip link add wg_br2 type wireguard
fi
# Linux interface parameters for wg_br2
#
sysctl -qw net/ipv6/conf/wg_br2/disable_ipv6=1
ip link set dev wg_br2 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br2 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51824 \
peer 9TZmTy1wikaJp8h0d6E9BYjuWnQA0pwX3s5D7cN7P2E= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.25:51820 \
  persistent-keepalive 25

ip link set dev wg_br2 up
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
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51825 \
peer 9TZmTy1wikaJp8h0d6E9BYjuWnQA0pwX3s5D7cN7P2E= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.29:51821 \
  persistent-keepalive 25

ip link set dev wg_br2_b up
if [ ! -e /sys/class/net/wg_br3 ]; then
  ip link add wg_br3 type wireguard
fi
# Linux interface parameters for wg_br3
#
sysctl -qw net/ipv6/conf/wg_br3/disable_ipv6=1
ip link set dev wg_br3 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br3 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51826 \
peer mYAoFtYCu/VHDtfvy30anGjNlunbj81aS7cCXK9SfgQ= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.33:51820 \
  persistent-keepalive 25

ip link set dev wg_br3 up
if [ ! -e /sys/class/net/wg_br3_b ]; then
  ip link add wg_br3_b type wireguard
fi
# Linux interface parameters for wg_br3_b
#
sysctl -qw net/ipv6/conf/wg_br3_b/disable_ipv6=1
ip link set dev wg_br3_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br3_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51827 \
peer mYAoFtYCu/VHDtfvy30anGjNlunbj81aS7cCXK9SfgQ= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.37:51821 \
  persistent-keepalive 25

ip link set dev wg_br3_b up
if [ ! -e /sys/class/net/wg_br4 ]; then
  ip link add wg_br4 type wireguard
fi
# Linux interface parameters for wg_br4
#
sysctl -qw net/ipv6/conf/wg_br4/disable_ipv6=1
ip link set dev wg_br4 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br4 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51828 \
peer 428L6E57YOEsN/yfzLlH2y9s1iJRlgARHUkFm/LzGUU= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.41:51820 \
  persistent-keepalive 25

ip link set dev wg_br4 up
if [ ! -e /sys/class/net/wg_br4_b ]; then
  ip link add wg_br4_b type wireguard
fi
# Linux interface parameters for wg_br4_b
#
sysctl -qw net/ipv6/conf/wg_br4_b/disable_ipv6=1
ip link set dev wg_br4_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br4_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51829 \
peer 428L6E57YOEsN/yfzLlH2y9s1iJRlgARHUkFm/LzGUU= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.45:51821 \
  persistent-keepalive 25

ip link set dev wg_br4_b up
if [ ! -e /sys/class/net/wg_br5 ]; then
  ip link add wg_br5 type wireguard
fi
# Linux interface parameters for wg_br5
#
sysctl -qw net/ipv6/conf/wg_br5/disable_ipv6=1
ip link set dev wg_br5 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br5 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51830 \
peer Biy8ok5qpvDhQTbPkPH2yxWncoh9zuno6zQVHfoB5SY= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.49:51820 \
  persistent-keepalive 25

ip link set dev wg_br5 up
if [ ! -e /sys/class/net/wg_br5_b ]; then
  ip link add wg_br5_b type wireguard
fi
# Linux interface parameters for wg_br5_b
#
sysctl -qw net/ipv6/conf/wg_br5_b/disable_ipv6=1
ip link set dev wg_br5_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br5_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51831 \
peer Biy8ok5qpvDhQTbPkPH2yxWncoh9zuno6zQVHfoB5SY= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.53:51821 \
  persistent-keepalive 25

ip link set dev wg_br5_b up
if [ ! -e /sys/class/net/wg_br6 ]; then
  ip link add wg_br6 type wireguard
fi
# Linux interface parameters for wg_br6
#
sysctl -qw net/ipv6/conf/wg_br6/disable_ipv6=1
ip link set dev wg_br6 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br6 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51832 \
peer As/tgQzIwcQK+y8vqtbqTji/b+aN1aVQ0UBNf+miIno= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.57:51820 \
  persistent-keepalive 25

ip link set dev wg_br6 up
if [ ! -e /sys/class/net/wg_br6_b ]; then
  ip link add wg_br6_b type wireguard
fi
# Linux interface parameters for wg_br6_b
#
sysctl -qw net/ipv6/conf/wg_br6_b/disable_ipv6=1
ip link set dev wg_br6_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br6_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51833 \
peer As/tgQzIwcQK+y8vqtbqTji/b+aN1aVQ0UBNf+miIno= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.61:51821 \
  persistent-keepalive 25

ip link set dev wg_br6_b up
if [ ! -e /sys/class/net/wg_br7 ]; then
  ip link add wg_br7 type wireguard
fi
# Linux interface parameters for wg_br7
#
sysctl -qw net/ipv6/conf/wg_br7/disable_ipv6=1
ip link set dev wg_br7 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br7 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51834 \
peer dC3hq0ygwhHjaINOTMoBt4xkTVt1Xkam009W1uIdVlI= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.65:51820 \
  persistent-keepalive 25

ip link set dev wg_br7 up
if [ ! -e /sys/class/net/wg_br7_b ]; then
  ip link add wg_br7_b type wireguard
fi
# Linux interface parameters for wg_br7_b
#
sysctl -qw net/ipv6/conf/wg_br7_b/disable_ipv6=1
ip link set dev wg_br7_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br7_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51835 \
peer dC3hq0ygwhHjaINOTMoBt4xkTVt1Xkam009W1uIdVlI= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.69:51821 \
  persistent-keepalive 25

ip link set dev wg_br7_b up
if [ ! -e /sys/class/net/wg_br8 ]; then
  ip link add wg_br8 type wireguard
fi
# Linux interface parameters for wg_br8
#
sysctl -qw net/ipv6/conf/wg_br8/disable_ipv6=1
ip link set dev wg_br8 mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br8 \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51836 \
peer FzafTkXCKGL8n87mm61v+K858sERiJ7c0aprS4gG+EU= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.73:51820 \
  persistent-keepalive 25

ip link set dev wg_br8 up
if [ ! -e /sys/class/net/wg_br8_b ]; then
  ip link add wg_br8_b type wireguard
fi
# Linux interface parameters for wg_br8_b
#
sysctl -qw net/ipv6/conf/wg_br8_b/disable_ipv6=1
ip link set dev wg_br8_b mtu 1420


#
# Setup VRF underlay if needed
#

wg set wg_br8_b \
  private-key <(printf '%s\n' '2GgVb0iNnxftqW6ZjrcdKqheSJnMZkl4GfHquaTrrmA=') \
  listen-port 51837 \
peer FzafTkXCKGL8n87mm61v+K858sERiJ7c0aprS4gG+EU= \
  allowed-ips 0.0.0.0/0 \
  endpoint 100.64.0.77:51821 \
  persistent-keepalive 25

ip link set dev wg_br8_b up
exit 0