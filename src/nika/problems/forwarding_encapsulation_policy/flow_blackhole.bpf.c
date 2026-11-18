#include <linux/bpf.h>
#include <linux/pkt_cls.h>

#define SEC(NAME) __attribute__((section(NAME), used))
static long (*bpf_skb_load_bytes)(struct __sk_buff *skb, __u32 offset,
                                  void *to, __u32 len) =
    (void *)BPF_FUNC_skb_load_bytes;

#ifndef SEED
#define SEED 0
#endif

static __always_inline __u32 mix(__u32 value, __u32 byte) {
    return (value ^ byte) * 16777619u;
}

/* Murmur3 finalizer: select on a fully mixed hash, since Linux picks even
 * ephemeral ports and FNV low bits follow the low input bits. */
static __always_inline __u32 avalanche(__u32 value) {
    value ^= value >> 16;
    value *= 0x85ebca6bu;
    value ^= value >> 13;
    value *= 0xc2b2ae35u;
    return value ^ (value >> 16);
}

/* Drop every forwarded packet of one quarter of TCP and UDP five-tuples.
 * Packets the device sends itself (routing protocols, ICMP errors) and ICMP
 * echo pass, as a forwarding-table fault leaves the CPU path alone. The
 * program and object names show in `tc filter show` and `bpftool prog`, so
 * they read like an ordinary egress classifier. */
SEC("classifier")
int tc_egress(struct __sk_buff *skb) {
    __u8 ip[20], ports[4];
    __u32 flow = 2166136261u ^ SEED;
    __u16 ethertype;
    __u8 ihl;

    if (!skb->ingress_ifindex ||
        bpf_skb_load_bytes(skb, 12, &ethertype, sizeof(ethertype)) ||
        __builtin_bswap16(ethertype) != 0x0800 ||
        bpf_skb_load_bytes(skb, 14, ip, sizeof(ip)))
        return TC_ACT_OK;
    ihl = (ip[0] & 0x0f) * 4;
    if ((ip[0] >> 4) != 4 || ihl < 20 || (ip[9] != 6 && ip[9] != 17))
        return TC_ACT_OK;
    /* Later fragments carry no ports. */
    if ((ip[6] & 0x1f) || ip[7])
        return TC_ACT_OK;
    if (bpf_skb_load_bytes(skb, 14 + ihl, ports, sizeof(ports)))
        return TC_ACT_OK;
    #pragma unroll
    for (int index = 12; index < 20; index++)
        flow = mix(flow, ip[index]);
    flow = mix(flow, ip[9]);
    #pragma unroll
    for (int index = 0; index < 4; index++)
        flow = mix(flow, ports[index]);
    return avalanche(flow) & 3 ? TC_ACT_OK : TC_ACT_SHOT;
}

char LICENSE[] SEC("license") = "GPL";
