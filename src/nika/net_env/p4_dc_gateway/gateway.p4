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
header int_mx_h {
    bit<8> type; bit<1> m; bit<1> e; bit<6> reserved;
    bit<8> originalProtocol; bit<16> flowId; bit<16> packetId;
}

struct headers { ethernet_h ethernet; ipv4_h ipv4; tcp_h tcp; udp_h udp; icmp_h icmp; int_mx_h int_mx; }
struct metadata {
    bit<16> srcPort; bit<16> dstPort; bit<8> role;
    bit<16> tcpLen;
    bit<8> tcpProtocol;
    bit<32> flowHash; bit<32> flowHash1; bit<32> flowHash2; bit<32> flowHash3;
    bit<32> packetHash; bit<1> watched; bit<16> intMtu;
    bit<16> faultLossThreshold; bit<32> ecnThreshold;
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
    @name("fault_drop") action faultDrop() { mark_to_drop(standard_metadata); }
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
    @name("set_fault_loss_threshold")
    action setFaultLossThreshold(bit<16> threshold) { meta.faultLossThreshold = threshold; }
    @name("internal_fault_loss_config")
    table internalFaultLossConfig {
        key = { standard_metadata.egress_spec: exact; }
        actions = { setFaultLossThreshold; NoAction; }
        size = 64;
        default_action = NoAction();
    }
    @name("int_watchlist") table intWatchlist {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { watchInt; NoAction; }
        size = 32;
        default_action = NoAction();
    }
    @name("internal_fault_drop")
    table internalFaultDrop {
        key = { hdr.ipv4.dstAddr: exact; }
        actions = { faultDrop; NoAction; }
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
    @name("lb_pool") table lbPool {
        key = { meta.lbPoolVersion: exact; meta.lbBucket: exact; }
        actions = { setPoolDip; NoAction; } size = 256; default_action = NoAction();
    }
    @name("icmp_frag_needed_drop") action icmpFragNeededDrop() { mark_to_drop(standard_metadata); }
    @name("icmp_frag_needed_acl") table icmpFragNeededAcl {
        key = { hdr.icmp.type: exact; hdr.icmp.code: exact; }
        actions = { icmpFragNeededDrop; NoAction; } size = 4; default_action = NoAction();
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
        meta.tcpLen = hdr.ipv4.totalLen - ((bit<16>)hdr.ipv4.ihl << 2);
        meta.tcpProtocol = hdr.ipv4.protocol;
        meta.intMtu = 0; meta.faultLossThreshold = 0;
        meta.watched = 0;
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
        hash(meta.packetHash, HashAlgorithm.crc32, (bit<32>)0,
             {standard_metadata.packet_length, hdr.ipv4.identification, meta.flowHash}, (bit<32>)65535);
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
                if (meta.lbDip != 0) { hdr.ipv4.dstAddr = meta.lbDip; }
            }
            if (hdr.ipv4.srcAddr == 32w0x0a00010b || hdr.ipv4.srcAddr == 32w0x0a00010c) { hdr.ipv4.srcAddr = 32w0x14000001; }
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
        // The L4 translation checksum covers the original TCP payload.  Do
        // not add INT between the IP and TCP headers on VIP traffic.
        if (meta.lbVip == 0) { intWatchlist.apply(); }
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
        /* Ordinary counters precede both private fault hooks. */
        internalFaultDrop.apply();
        internalFaultLossConfig.apply();
        if (meta.faultLossThreshold > 0 && meta.packetHash[15:0] < meta.faultLossThreshold) { faultDrop(); }
        // Apply this terminal ACL after routing: ipv4Lpm.forward otherwise
        // overwrites the egress drop set by icmpFragNeededDrop.
        if (meta.role == ROLE_GATEWAY && hdr.icmp.isValid()) { icmpFragNeededAcl.apply(); }
    }
}

control GatewayEgress(inout headers hdr, inout metadata meta,
                      inout standard_metadata_t standard_metadata) {
    @name("egress_port_counter") counter(64, CounterType.packets_and_bytes) egressCounter;
    @name("queue_occupancy") register<bit<32>>(64) queueOccupancy;
    @name("set_ecn_threshold") action setEcnThreshold(bit<32> threshold) { meta.ecnThreshold = threshold; }
    @name("ecn_config") table ecnConfig {
        key = { standard_metadata.egress_port: exact; }
        actions = { setEcnThreshold; NoAction; }
        size = 64;
        default_action = NoAction();
    }
    apply {
        egressCounter.count((bit<32>)standard_metadata.egress_port);
        queueOccupancy.write((bit<32>)standard_metadata.egress_port,
                             (bit<32>)standard_metadata.enq_qdepth);
        meta.ecnThreshold = 0;
        ecnConfig.apply();
        if (meta.ecnThreshold > 0 && (bit<32>)standard_metadata.enq_qdepth >= meta.ecnThreshold &&
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
        update_checksum_with_payload(hdr.ipv4.isValid() && hdr.tcp.isValid(),
        {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, 8w0, meta.tcpProtocol,
         meta.tcpLen, hdr.tcp.srcPort, hdr.tcp.dstPort, hdr.tcp.seqNo,
         hdr.tcp.ackNo, hdr.tcp.dataOffset, hdr.tcp.res, hdr.tcp.flags,
         hdr.tcp.window, 16w0, hdr.tcp.urgentPtr},
        hdr.tcp.checksum, HashAlgorithm.csum16); }
}
control GatewayDeparser(packet_out packet, in headers hdr) {
    apply { packet.emit(hdr.ethernet); packet.emit(hdr.ipv4); packet.emit(hdr.int_mx); packet.emit(hdr.tcp); packet.emit(hdr.udp); packet.emit(hdr.icmp); }
}
V1Switch(GatewayParser(), Verify(), GatewayIngress(), GatewayEgress(), Compute(), GatewayDeparser()) main;
