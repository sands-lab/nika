#include <linux/bpf.h>
#include <linux/pkt_cls.h>

#define SEC(NAME) __attribute__((section(NAME), used))
static long (*bpf_skb_load_bytes)(struct __sk_buff *skb, __u32 offset,
                                  void *to, __u32 len) =
    (void *)BPF_FUNC_skb_load_bytes;
static long (*bpf_skb_store_bytes)(struct __sk_buff *skb, __u32 offset,
                                   const void *from, __u32 len, __u64 flags) =
    (void *)BPF_FUNC_skb_store_bytes;

#ifndef SEED
#define SEED 0
#endif

static __always_inline __u32 mix(__u32 value, __u32 byte) {
    return (value ^ byte) * 16777619u;
}

SEC("classifier")
int switch_bitflip(struct __sk_buff *skb) {
    __u8 ip[20], tcp[20], byte;
    __u32 flow = 2166136261u ^ SEED, ip_off = 14, tcp_off, payload_off;
    __u16 ethertype, src_port, dst_port;
    __u8 ihl, doff;
    __u32 seq;

    if (bpf_skb_load_bytes(skb, 12, &ethertype, sizeof(ethertype)) ||
        __builtin_bswap16(ethertype) != 0x0800 ||
        bpf_skb_load_bytes(skb, ip_off, ip, sizeof(ip)))
        return TC_ACT_OK;
    ihl = (ip[0] & 0x0f) * 4;
    if ((ip[0] >> 4) != 4 || ihl < 20 || ip[9] != 6)
        return TC_ACT_OK;
    tcp_off = ip_off + ihl;
    if (bpf_skb_load_bytes(skb, tcp_off, tcp, sizeof(tcp)))
        return TC_ACT_OK;
    doff = (tcp[12] >> 4) * 4;
    payload_off = tcp_off + doff;
    if (doff < 20 || skb->len < payload_off + 33)
        return TC_ACT_OK;
    #pragma unroll
    for (int index = 12; index < 20; index++)
        flow = mix(flow, ip[index]);
    src_port = ((__u16)tcp[0] << 8) | tcp[1];
    dst_port = ((__u16)tcp[2] << 8) | tcp[3];
    flow = mix(mix(flow, src_port), dst_port);
    seq = ((__u32)tcp[4] << 24) | ((__u32)tcp[5] << 16) |
          ((__u32)tcp[6] << 8) | tcp[7];
    /* Affect half the stable five-tuples and one sixteenth of their packets. */
    if (flow & 1 || (mix(flow, seq >> 8) & 15))
        return TC_ACT_OK;
    if (bpf_skb_load_bytes(skb, payload_off + 32, &byte, 1))
        return TC_ACT_OK;
    byte ^= 1;
    bpf_skb_store_bytes(skb, payload_off + 32, &byte, 1, 0);
    return TC_ACT_OK;
}

char LICENSE[] SEC("license") = "GPL";
