/* -*- P4_16 -*- */
/* SPDX-License-Identifier: Apache-2.0 */
#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;
const bit<8> PROTO_TCP = 6;
const bit<8> PROTO_UDP = 17;
const bit<8> PROTO_ICMP = 1;
const bit<8> PROTO_INT_MX = 253;
const bit<8> ROLE_GATEWAY = 1;
const bit<8> ROLE_LEAF = 3;
const bit<32> VIP_ADDR = 0x14000001;      /* 20.0.0.1 */
const bit<16> VIP_PORT = 80;
const bit<32> CLIENT_GW_ADDR = 0xc0000201; /* 192.0.2.1 */

typedef bit<9> port_t;
typedef bit<48> mac_t;
typedef bit<32> ipv4_t;

header ethernet_h { mac_t dstAddr; mac_t srcAddr; bit<16> etherType; }
header ipv4_h {
    bit<4> version; bit<4> ihl; bit<8> diffserv; bit<16> totalLen;
    bit<16> identification; bit<3> flags; bit<13> fragOffset; bit<8> ttl;
    bit<8> protocol; bit<16> hdrChecksum; ipv4_t srcAddr; ipv4_t dstAddr;
}
header tcp_h {
    bit<16> srcPort; bit<16> dstPort; bit<32> seqNo; bit<32> ackNo;
    bit<4> dataOffset; bit<3> res; bit<9> flags; bit<16> window;
    bit<16> checksum; bit<16> urgentPtr;
}
header udp_h { bit<16> srcPort; bit<16> dstPort; bit<16> length; bit<16> checksum; }
header icmp_h { bit<8> type; bit<8> code; bit<16> checksum; bit<32> rest; }
header l4_head_h { bit<64> data; }
header int_mx_h {
    bit<8> type; bit<1> m; bit<1> e; bit<6> reserved;
    bit<8> originalProtocol; bit<16> flowId; bit<16> packetId;
}

struct headers {
    ethernet_h ethernet; ipv4_h ipv4; tcp_h tcp; udp_h udp; icmp_h icmp; int_mx_h int_mx;
    ipv4_h icmp_quote_ipv4; l4_head_h icmp_quote_l4;
}
struct metadata {
    bit<16> srcPort; bit<16> dstPort; bit<8> role;
    bit<32> flowHash; bit<32> flowHash1; bit<32> flowHash2; bit<32> flowHash3;
    bit<1> watched; bit<16> intMtu;
    bit<16> egressMtu; bit<1> icmpSent; bit<32> ecnThreshold;
    bit<32> lbFlowIndex; ipv4_t lbFlowDip; mac_t rxSrcMac; mac_t rxDstMac;
    bit<1> lbVip; bit<1> lbConnHit; bit<1> lbTransitHit;
    bit<8> lbVersion; bit<8> lbPoolVersion; bit<8> lbBucket;
    ipv4_t lbDip;
}

parser GatewayParser(packet_in packet, out headers hdr, inout metadata meta,
                     inout standard_metadata_t standard_metadata) {
    state start { packet.extract(hdr.ethernet); transition select(hdr.ethernet.etherType) { TYPE_IPV4: ipv4; default: accept; } }
    state ipv4 { packet.extract(hdr.ipv4); transition select(hdr.ipv4.protocol) { PROTO_TCP: tcp; PROTO_UDP: udp; PROTO_ICMP: icmp; PROTO_INT_MX: int_mx; default: accept; } }
    state int_mx { packet.extract(hdr.int_mx); transition select(hdr.int_mx.originalProtocol) { PROTO_TCP: tcp; PROTO_UDP: udp; default: accept; } }
    state tcp { packet.extract(hdr.tcp); transition accept; }
    state udp { packet.extract(hdr.udp); transition accept; }
    state icmp { packet.extract(hdr.icmp); transition accept; }
}
control Verify(inout headers hdr, inout metadata meta) { apply { } }

control GatewayIngress(inout headers hdr, inout metadata meta,
                       inout standard_metadata_t standard_metadata) {
    @name("ingress_port_counter") counter(64, CounterType.packets_and_bytes) ingressCounter;
    @name("flow_syn_total") counter(1, CounterType.packets_and_bytes) synTotal;
    @name("flow_non_syn_total") counter(1, CounterType.packets_and_bytes) nonSynTotal;
    @name("flow_syn_count_0") register<bit<32>>(4096) synCount0;
    @name("flow_syn_count_1") register<bit<32>>(4096) synCount1;
    @name("flow_syn_count_2") register<bit<32>>(4096) synCount2;
    @name("flow_syn_count_3") register<bit<32>>(4096) synCount3;
    @name("flow_non_syn_count_0") register<bit<32>>(4096) nonSynCount0;
    @name("flow_non_syn_count_1") register<bit<32>>(4096) nonSynCount1;
    @name("flow_non_syn_count_2") register<bit<32>>(4096) nonSynCount2;
    @name("flow_non_syn_count_3") register<bit<32>>(4096) nonSynCount3;
    @name("ecmp_selector") action_selector(HashAlgorithm.crc16, 32w256, 32w16) ecmpSelector;
    @name("drop") action drop() { mark_to_drop(standard_metadata); }
    @name("watch_int") action watchInt() { meta.watched = 1; }
    @name("set_role") action setRole(bit<8> role) { meta.role = role; }
    @name("runtime_role") table runtimeRole {
        actions = { setRole; }
        default_action = setRole(0);
    }
    @name("set_int_mtu") action setIntMtu(bit<16> mtu) { meta.intMtu = mtu; }
    @name("int_mtu_config") table intMtuConfig {
        key = { standard_metadata.egress_spec: exact; }
        actions = { setIntMtu; NoAction; }
        size = 64;
        default_action = NoAction();
    }
    @name("set_egress_mtu") action setEgressMtu(bit<16> mtu) { meta.egressMtu = mtu; }
    @name("egress_mtu") table egressMtu {
        key = { standard_metadata.egress_spec: exact; }
        actions = { setEgressMtu; NoAction; }
        size = 64;
        default_action = NoAction();
    }
    @name("int_watchlist") table intWatchlist {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { watchInt; NoAction; }
        size = 32;
        default_action = NoAction();
    }
    @name("set_lb_vip") action setLbVip(bit<8> version) { meta.lbVip = 1; meta.lbVersion = version; }
    @name("lb_vip") table lbVip {
        key = { hdr.ipv4.dstAddr: exact; meta.dstPort: exact; hdr.ipv4.protocol: exact; }
        actions = { setLbVip; NoAction; } size = 8; default_action = NoAction();
    }
    @name("set_lb_dip") action setLbDip(ipv4_t dip) { meta.lbDip = dip; meta.lbConnHit = 1; }
    @name("lb_conn_table") table lbConnTable {
        key = { hdr.ipv4.srcAddr: exact; hdr.ipv4.dstAddr: exact; hdr.ipv4.protocol: exact; meta.srcPort: exact; meta.dstPort: exact; }
        actions = { setLbDip; NoAction; } size = 256; default_action = NoAction();
    }
    @name("set_transit_version") action setTransitVersion(bit<8> version) { meta.lbPoolVersion = version; meta.lbTransitHit = 1; }
    @name("lb_transit_table") table lbTransitTable {
        key = { hdr.ipv4.srcAddr: exact; hdr.ipv4.dstAddr: exact; hdr.ipv4.protocol: exact; meta.srcPort: exact; meta.dstPort: exact; }
        actions = { setTransitVersion; NoAction; } size = 256; default_action = NoAction();
    }
    @name("set_pool_dip") action setPoolDip(ipv4_t dip) { meta.lbDip = dip; }
    // DIP of each translated VIP flow, indexed by the client address and
    // port, so only replies of those flows get the VIP source back.
    @name("lb_flow_dip") register<ipv4_t>(4096) lbFlowDip;
    @name("lb_pool") table lbPool {
        key = { meta.lbPoolVersion: exact; meta.lbBucket: exact; }
        actions = { setPoolDip; NoAction; } size = 256; default_action = NoAction();
    }
    // RFC 1624 incremental update: short frames carry Ethernet padding that a
    // full recomputation over the parsed payload would wrongly include.
    action rewriteTcpAddr(inout ipv4_t field, ipv4_t value) {
        bit<32> sum = (bit<32>)(~hdr.tcp.checksum)
            + (bit<32>)(~field[31:16]) + (bit<32>)(~field[15:0])
            + (bit<32>)value[31:16] + (bit<32>)value[15:0];
        sum = (sum & 0xffff) + (sum >> 16);
        sum = (sum & 0xffff) + (sum >> 16);
        hdr.tcp.checksum = ~sum[15:0];
        field = value;
    }
    @name("icmp_acl") table icmpAcl {
        key = { hdr.icmp.type: exact; hdr.icmp.code: exact; }
        actions = { drop; NoAction; } size = 8; default_action = NoAction();
    }
    @name("ipv4_forward") action forward(mac_t srcAddr, mac_t dstAddr, port_t port) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = srcAddr; hdr.ethernet.dstAddr = dstAddr;
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
    }
    @name("ipv4_lpm") table ipv4Lpm {
        key = {
            hdr.ipv4.dstAddr: lpm; hdr.ipv4.srcAddr: selector;
            hdr.ipv4.protocol: selector; meta.srcPort: selector; meta.dstPort: selector;
        }
        actions = { forward; drop; }
        implementation = ecmpSelector;
        size = 256;
    }

    apply {
        ingressCounter.count((bit<32>)standard_metadata.ingress_port);
        if (!hdr.ipv4.isValid() || hdr.ipv4.ttl <= 1) { drop(); return; }
        meta.srcPort = 0; meta.dstPort = 0;
        meta.intMtu = 0; meta.egressMtu = 0; meta.icmpSent = 0;
        meta.watched = 0; meta.lbFlowIndex = 0; meta.lbFlowDip = 0;
        meta.rxSrcMac = hdr.ethernet.srcAddr; meta.rxDstMac = hdr.ethernet.dstAddr;
        meta.lbVip = 0; meta.lbConnHit = 0; meta.lbTransitHit = 0;
        meta.lbVersion = 0; meta.lbPoolVersion = 0; meta.lbBucket = 0; meta.lbDip = 0;
        if (hdr.tcp.isValid()) { meta.srcPort = hdr.tcp.srcPort; meta.dstPort = hdr.tcp.dstPort; }
        else if (hdr.udp.isValid()) { meta.srcPort = hdr.udp.srcPort; meta.dstPort = hdr.udp.dstPort; }
        hash(meta.flowHash, HashAlgorithm.crc32, (bit<32>)0,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, hdr.ipv4.protocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        hash(meta.flowHash1, HashAlgorithm.crc32, (bit<32>)17,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, hdr.ipv4.protocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        hash(meta.flowHash2, HashAlgorithm.crc16, (bit<32>)31,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, hdr.ipv4.protocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        hash(meta.flowHash3, HashAlgorithm.csum16, (bit<32>)47,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, hdr.ipv4.protocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        runtimeRole.apply();
        if (meta.role == ROLE_GATEWAY && hdr.tcp.isValid()) {
            lbVip.apply();
            if (meta.lbVip == 1) {
                lbConnTable.apply();
                if (meta.lbConnHit == 0) {
                    meta.lbPoolVersion = meta.lbVersion;
                    lbTransitTable.apply();
                    hash(meta.lbBucket, HashAlgorithm.crc16, (bit<8>)0,
                         {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, meta.srcPort, meta.dstPort}, (bit<8>)64);
                    lbPool.apply();
                }
                if (meta.lbDip != 0) {
                    hash(meta.lbFlowIndex, HashAlgorithm.crc32, (bit<32>)0,
                         {hdr.ipv4.srcAddr, meta.srcPort}, (bit<32>)4096);
                    lbFlowDip.write(meta.lbFlowIndex, meta.lbDip);
                    rewriteTcpAddr(hdr.ipv4.dstAddr, meta.lbDip);
                }
            } else if (hdr.tcp.flags[1:1] == 1 && hdr.tcp.flags[4:4] == 0) {
                // A new connection that bypasses the VIP reuses no VIP mapping.
                hash(meta.lbFlowIndex, HashAlgorithm.crc32, (bit<32>)0,
                     {hdr.ipv4.srcAddr, meta.srcPort}, (bit<32>)4096);
                lbFlowDip.write(meta.lbFlowIndex, 0);
            }
        }
        if (meta.role == ROLE_LEAF && hdr.int_mx.isValid()) {
            hdr.ipv4.protocol = hdr.int_mx.originalProtocol;
            hdr.ipv4.totalLen = hdr.ipv4.totalLen - 7;
            hdr.int_mx.setInvalid();
        }
        if (meta.role == ROLE_GATEWAY && hdr.tcp.isValid()) {
            bit<32> c0; bit<32> c1; bit<32> c2; bit<32> c3;
            if (hdr.tcp.flags[1:1] == 1) {
                synTotal.count(0);
                synCount0.read(c0, meta.flowHash); synCount0.write(meta.flowHash, c0 + 1);
                synCount1.read(c1, meta.flowHash1); synCount1.write(meta.flowHash1, c1 + 1);
                synCount2.read(c2, meta.flowHash2); synCount2.write(meta.flowHash2, c2 + 1);
                synCount3.read(c3, meta.flowHash3); synCount3.write(meta.flowHash3, c3 + 1);
            } else {
                nonSynTotal.count(0);
                nonSynCount0.read(c0, meta.flowHash); nonSynCount0.write(meta.flowHash, c0 + 1);
                nonSynCount1.read(c1, meta.flowHash1); nonSynCount1.write(meta.flowHash1, c1 + 1);
                nonSynCount2.read(c2, meta.flowHash2); nonSynCount2.write(meta.flowHash2, c2 + 1);
                nonSynCount3.read(c3, meta.flowHash3); nonSynCount3.write(meta.flowHash3, c3 + 1);
            }
        }
        ipv4Lpm.apply();
        egressMtu.apply();
        // Path MTU discovery (RFC 1191): a DF packet larger than the egress
        // MTU becomes ICMP Fragmentation Needed back to its source, quoting
        // the IP header and first 8 payload bytes as received.
        if (meta.role == ROLE_GATEWAY && meta.egressMtu != 0
            && hdr.ipv4.flags[1:1] == 1 && hdr.ipv4.totalLen > meta.egressMtu
            && !hdr.int_mx.isValid()
            && (hdr.tcp.isValid() || hdr.udp.isValid()
                || (hdr.icmp.isValid() && hdr.icmp.type != 3))) {
            hdr.icmp_quote_ipv4 = hdr.ipv4;
            hdr.icmp_quote_ipv4.ttl = hdr.ipv4.ttl + 1;
            hdr.icmp_quote_l4.setValid();
            if (hdr.tcp.isValid()) {
                hdr.icmp_quote_l4.data = hdr.tcp.srcPort ++ hdr.tcp.dstPort ++ hdr.tcp.seqNo;
            } else if (hdr.udp.isValid()) {
                hdr.icmp_quote_l4.data = hdr.udp.srcPort ++ hdr.udp.dstPort
                    ++ hdr.udp.length ++ hdr.udp.checksum;
            } else {
                hdr.icmp_quote_l4.data = hdr.icmp.type ++ hdr.icmp.code
                    ++ hdr.icmp.checksum ++ hdr.icmp.rest;
            }
            hdr.tcp.setInvalid(); hdr.udp.setInvalid();
            hdr.icmp.setValid();
            hdr.icmp.type = 3; hdr.icmp.code = 4; hdr.icmp.checksum = 0;
            hdr.icmp.rest = (bit<32>)meta.egressMtu;
            hdr.ipv4.dstAddr = hdr.ipv4.srcAddr; hdr.ipv4.srcAddr = CLIENT_GW_ADDR;
            hdr.ipv4.diffserv = 0; hdr.ipv4.totalLen = 56; hdr.ipv4.identification = 0;
            hdr.ipv4.flags = 0; hdr.ipv4.fragOffset = 0; hdr.ipv4.ttl = 64;
            hdr.ipv4.protocol = PROTO_ICMP;
            hdr.ethernet.srcAddr = meta.rxDstMac; hdr.ethernet.dstAddr = meta.rxSrcMac;
            standard_metadata.egress_spec = standard_metadata.ingress_port;
            truncate(70);
            meta.icmpSent = 1;
        }
        // Replies of translated VIP flows leave with the VIP as source.
        if (meta.role == ROLE_GATEWAY && hdr.tcp.isValid() && meta.lbVip == 0
            && hdr.tcp.srcPort == VIP_PORT) {
            hash(meta.lbFlowIndex, HashAlgorithm.crc32, (bit<32>)0,
                 {hdr.ipv4.dstAddr, meta.dstPort}, (bit<32>)4096);
            lbFlowDip.read(meta.lbFlowDip, meta.lbFlowIndex);
            if (meta.lbFlowDip == hdr.ipv4.srcAddr) { rewriteTcpAddr(hdr.ipv4.srcAddr, VIP_ADDR); }
        }
        // The L4 translation checksum covers the original TCP payload.  Do
        // not add INT between the IP and TCP headers on VIP traffic.
        if (meta.lbVip == 0 && meta.icmpSent == 0) { intWatchlist.apply(); }
        intMtuConfig.apply();
        if (meta.role == ROLE_GATEWAY && meta.watched == 1 && !hdr.int_mx.isValid()) {
            if (meta.intMtu == 0 || standard_metadata.packet_length + 7 <= (bit<32>)meta.intMtu) {
                hdr.int_mx.setValid(); hdr.int_mx.type = 1; hdr.int_mx.m = 0;
                hdr.int_mx.e = 0; hdr.int_mx.reserved = 0;
                hdr.int_mx.originalProtocol = hdr.ipv4.protocol;
                hdr.int_mx.flowId = meta.flowHash[15:0];
                hdr.int_mx.packetId = hdr.ipv4.identification;
                hdr.ipv4.protocol = PROTO_INT_MX;
                hdr.ipv4.totalLen = hdr.ipv4.totalLen + 7;
            }
        }
        // Apply this terminal ACL after routing: ipv4Lpm.forward otherwise
        // overwrites the egress drop set by icmpFragNeededDrop.
        if (meta.role == ROLE_GATEWAY && hdr.icmp.isValid()) { icmpAcl.apply(); }
    }
}

control GatewayEgress(inout headers hdr, inout metadata meta,
                      inout standard_metadata_t standard_metadata) {
    @name("egress_port_counter") counter(64, CounterType.packets_and_bytes) egressCounter;
    // BMv2 drains its egress queue as fast as the CPU allows, so enq_qdepth
    // stays near zero. A per-port virtual queue drained at one packet per
    // 16384 us (~61 pps, below the ~120 pps of INT traffic a logging BMv2
    // forwards) and capped at BMv2's default 64-packet queue gives ECN a
    // finite port rate.
    // It only drives marking; nothing is dropped.
    @name("queue_occupancy") register<bit<32>>(64) queueOccupancy;
    register<bit<48>>(64) queueDrainedAt;
    @name("set_ecn_threshold") action setEcnThreshold(bit<32> threshold) { meta.ecnThreshold = threshold; }
    @name("ecn_config") table ecnConfig {
        key = { standard_metadata.egress_port: exact; }
        actions = { setEcnThreshold; NoAction; }
        size = 64;
        default_action = NoAction();
    }
    apply {
        egressCounter.count((bit<32>)standard_metadata.egress_port);
        bit<32> port = (bit<32>)standard_metadata.egress_port;
        bit<48> now = standard_metadata.egress_global_timestamp;
        bit<32> depth; bit<48> drainedAt;
        queueOccupancy.read(depth, port);
        queueDrainedAt.read(drainedAt, port);
        bit<32> drained = (bit<32>)((now - drainedAt) >> 14);
        if (drained >= depth) { depth = 0; drainedAt = now; }
        else { depth = depth - drained; drainedAt = drainedAt + ((bit<48>)drained << 14); }
        if (depth < 64) { depth = depth + 1; }
        queueOccupancy.write(port, depth);
        queueDrainedAt.write(port, drainedAt);
        meta.ecnThreshold = 0;
        ecnConfig.apply();
        if (meta.ecnThreshold > 0 && depth >= meta.ecnThreshold &&
            (hdr.ipv4.diffserv[1:0] == 1 || hdr.ipv4.diffserv[1:0] == 2)) {
            hdr.ipv4.diffserv[1:0] = 3;
        }
    }
}
control Compute(inout headers hdr, inout metadata meta) {
    apply { update_checksum(hdr.ipv4.isValid(),
        {hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.diffserv, hdr.ipv4.totalLen,
         hdr.ipv4.identification, hdr.ipv4.flags, hdr.ipv4.fragOffset,
         hdr.ipv4.ttl, hdr.ipv4.protocol, hdr.ipv4.srcAddr, hdr.ipv4.dstAddr},
        hdr.ipv4.hdrChecksum, HashAlgorithm.csum16);
        update_checksum(hdr.icmp_quote_ipv4.isValid(),
        {hdr.icmp.type, hdr.icmp.code, hdr.icmp.rest,
         hdr.icmp_quote_ipv4.version, hdr.icmp_quote_ipv4.ihl, hdr.icmp_quote_ipv4.diffserv,
         hdr.icmp_quote_ipv4.totalLen, hdr.icmp_quote_ipv4.identification,
         hdr.icmp_quote_ipv4.flags, hdr.icmp_quote_ipv4.fragOffset, hdr.icmp_quote_ipv4.ttl,
         hdr.icmp_quote_ipv4.protocol, hdr.icmp_quote_ipv4.hdrChecksum,
         hdr.icmp_quote_ipv4.srcAddr, hdr.icmp_quote_ipv4.dstAddr, hdr.icmp_quote_l4.data},
        hdr.icmp.checksum, HashAlgorithm.csum16); }
}
control GatewayDeparser(packet_out packet, in headers hdr) {
    apply { packet.emit(hdr.ethernet); packet.emit(hdr.ipv4); packet.emit(hdr.int_mx); packet.emit(hdr.tcp); packet.emit(hdr.udp); packet.emit(hdr.icmp);
             packet.emit(hdr.icmp_quote_ipv4); packet.emit(hdr.icmp_quote_l4); }
}
V1Switch(GatewayParser(), Verify(), GatewayIngress(), GatewayEgress(), Compute(), GatewayDeparser()) main;
