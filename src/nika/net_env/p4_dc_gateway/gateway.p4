/* -*- P4_16 -*- */
/* SPDX-License-Identifier: Apache-2.0 */
#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;
const bit<8> PROTO_TCP = 6;
const bit<8> PROTO_UDP = 17;
const bit<8> PROTO_ICMP = 1;
const bit<16> INT_UDP_PORT = 32767;
const bit<32> REPORT_SESSION = 500;
const bit<8> ROLE_GATEWAY = 1;
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
// P4.org INT v2.1: UDP encapsulation, MD header, baseline instructions 0-3.
header int_shim_h {
    bit<4> type; bit<2> nextProtoType; bit<2> reserved;
    bit<8> length; bit<8> reserved2; bit<8> originalProtocol;
}
header int_md_h {
    bit<4> version; bit<1> d; bit<1> e; bit<1> m; bit<12> reserved;
    bit<5> hopMl; bit<8> remaining; bit<16> instructions;
    bit<16> domain; bit<16> dsInstructions; bit<16> dsFlags;
}
header int_hop_h {
    bit<32> switchId; bit<16> ingressPort; bit<16> egressPort;
    bit<32> latency; bit<8> queueId; bit<24> queueDepth;
}
// P4.org Telemetry Report v2.0, Inner Only, embedded IPv4 INT-MD packet.
header report_group_h {
    bit<4> version; bit<6> hwId; bit<22> sequence; bit<32> nodeId;
}
header report_h {
    bit<4> repType; bit<4> inType; bit<8> length;
    bit<8> mdLength; bit<1> d; bit<1> q; bit<1> f; bit<1> i; bit<4> reserved;
}

struct headers {
    ethernet_h ethernet; ipv4_h ipv4; tcp_h tcp; udp_h udp; icmp_h icmp;
    udp_h int_udp; int_shim_h int_shim; int_md_h int_md; int_hop_h[8] hops;
    // Header snapshots survive BMv2 E2E cloning but are never deparsed.
    ipv4_h saved_ipv4; udp_h saved_int_udp; int_shim_h saved_int_shim;
    int_md_h saved_int_md; int_hop_h[8] saved_hops;
    ethernet_h report_ethernet; ipv4_h report_ipv4; udp_h report_udp;
    report_group_h report_group; report_h report;
    ipv4_h icmp_quote_ipv4; l4_head_h icmp_quote_l4;
}
struct metadata {
    bit<16> srcPort; bit<16> dstPort; bit<8> role;
    bit<8> ipProtocol; bit<8> hopsToParse;
    bit<32> switchId; ipv4_t reportSrc; mac_t collectorMac;
    bit<32> flowHash; bit<32> flowHash1; bit<32> flowHash2; bit<32> flowHash3;
    bit<1> watched; bit<1> intSource; bit<1> intSink; bit<16> intMtu;
    bit<16> egressMtu; bit<1> icmpSent; bit<32> ecnThreshold;
    bit<32> lbFlowIndex; ipv4_t lbFlowDip; mac_t rxSrcMac; mac_t rxDstMac;
    bit<1> lbVip; bit<1> lbConnHit; bit<1> lbTransitHit;
    bit<8> lbVersion; bit<8> lbPoolVersion; bit<8> lbBucket;
    ipv4_t lbDip;
}

parser GatewayParser(packet_in packet, out headers hdr, inout metadata meta,
                     inout standard_metadata_t standard_metadata) {
    state start { packet.extract(hdr.ethernet); transition select(hdr.ethernet.etherType) { TYPE_IPV4: ipv4; default: accept; } }
    state ipv4 { packet.extract(hdr.ipv4); transition select(hdr.ipv4.protocol) { PROTO_TCP: tcp; PROTO_UDP: udp; PROTO_ICMP: icmp; default: accept; } }
    state int_shim {
        hdr.int_udp = hdr.udp; hdr.udp.setInvalid();
        packet.extract(hdr.int_shim); packet.extract(hdr.int_md);
        verify(hdr.int_shim.type == 1 && hdr.int_shim.nextProtoType == 2
            && hdr.int_md.version == 2 && hdr.int_md.hopMl == 4
            && (hdr.int_md.instructions & 0xff81) == 0xf000
            && hdr.int_shim.length >= 3 && hdr.int_shim.length <= 35
            && ((hdr.int_shim.length - 3) & 3) == 0,
            error.NoMatch);
        meta.hopsToParse = (hdr.int_shim.length - 3) >> 2;
        transition select(meta.hopsToParse) { 0: int_payload; default: int_hop; }
    }
    state int_hop {
        packet.extract(hdr.hops.next);
        meta.hopsToParse = meta.hopsToParse - 1;
        transition select(meta.hopsToParse) { 0: int_payload; default: int_hop; }
    }
    state int_payload {
        transition select(hdr.int_shim.originalProtocol) {
            PROTO_TCP: tcp; PROTO_UDP: original_udp; default: accept;
        }
    }
    state original_udp { packet.extract(hdr.udp); transition accept; }
    state tcp { packet.extract(hdr.tcp); transition accept; }
    state udp { packet.extract(hdr.udp); transition select(hdr.udp.dstPort) { INT_UDP_PORT: int_shim; default: accept; } }
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
    @name("int_source") action intSource() { meta.intSource = 1; }
    @name("int_source_port") table intSourcePort {
        key = { standard_metadata.ingress_port: exact; }
        actions = { intSource; NoAction; } size = 64; default_action = NoAction();
    }
    @name("int_watchlist") table intWatchlist {
        key = {
            hdr.ipv4.srcAddr: ternary; hdr.ipv4.dstAddr: ternary;
            meta.ipProtocol: ternary; meta.srcPort: ternary; meta.dstPort: ternary;
        }
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
    // Each gateway serves one client. Index its full TCP port space so a
    // direct connection cannot clear an unrelated VIP connection's mapping.
    @name("lb_flow_dip") register<ipv4_t>(65536) lbFlowDip;
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
            hdr.ipv4.dstAddr: ternary; hdr.ipv4.srcAddr: selector;
            hdr.ipv4.protocol: selector; meta.srcPort: selector; meta.dstPort: selector;
        }
        actions = { forward; drop; }
        implementation = ecmpSelector;
        size = 256;
    }

    apply {
        ingressCounter.count((bit<32>)standard_metadata.ingress_port);
        if (standard_metadata.parser_error != error.NoError || !hdr.ipv4.isValid() || hdr.ipv4.ttl <= 1) { drop(); return; }
        meta.srcPort = 0; meta.dstPort = 0;
        meta.ipProtocol = hdr.ipv4.protocol;
        if (hdr.int_shim.isValid()) { meta.ipProtocol = hdr.int_shim.originalProtocol; }
        meta.intMtu = 0; meta.egressMtu = 0; meta.icmpSent = 0;
        meta.watched = 0; meta.lbFlowIndex = 0; meta.lbFlowDip = 0;
        meta.rxSrcMac = hdr.ethernet.srcAddr; meta.rxDstMac = hdr.ethernet.dstAddr;
        meta.lbVip = 0; meta.lbConnHit = 0; meta.lbTransitHit = 0;
        meta.lbVersion = 0; meta.lbPoolVersion = 0; meta.lbBucket = 0; meta.lbDip = 0;
        if (hdr.tcp.isValid()) { meta.srcPort = hdr.tcp.srcPort; meta.dstPort = hdr.tcp.dstPort; }
        else if (hdr.udp.isValid()) { meta.srcPort = hdr.udp.srcPort; meta.dstPort = hdr.udp.dstPort; }
        hash(meta.flowHash, HashAlgorithm.crc32, (bit<32>)0,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, meta.ipProtocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        hash(meta.flowHash1, HashAlgorithm.crc32, (bit<32>)17,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, meta.ipProtocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        hash(meta.flowHash2, HashAlgorithm.crc16, (bit<32>)31,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, meta.ipProtocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        hash(meta.flowHash3, HashAlgorithm.csum16, (bit<32>)47,
             {hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, meta.ipProtocol, meta.srcPort, meta.dstPort}, (bit<32>)4096);
        meta.intSource = 0; intSourcePort.apply();
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
                    meta.lbFlowIndex = (bit<32>)meta.srcPort;
                    lbFlowDip.write(meta.lbFlowIndex, meta.lbDip);
                    rewriteTcpAddr(hdr.ipv4.dstAddr, meta.lbDip);
                }
            } else if (hdr.tcp.flags[1:1] == 1 && hdr.tcp.flags[4:4] == 0) {
                // Clear stale state only for this direct connection's backend.
                meta.lbFlowIndex = (bit<32>)meta.srcPort;
                lbFlowDip.read(meta.lbFlowDip, meta.lbFlowIndex);
                if (meta.lbFlowDip == hdr.ipv4.dstAddr) {
                    lbFlowDip.write(meta.lbFlowIndex, 0);
                }
            }
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
            && !hdr.int_shim.isValid()
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
            meta.lbFlowIndex = (bit<32>)meta.dstPort;
            lbFlowDip.read(meta.lbFlowDip, meta.lbFlowIndex);
            if (meta.lbFlowDip == hdr.ipv4.srcAddr) { rewriteTcpAddr(hdr.ipv4.srcAddr, VIP_ADDR); }
        }
        // The L4 translation checksum covers the original TCP payload.  Do
        // not add INT between the IP and TCP headers on VIP traffic.
        if (meta.lbVip == 0 && meta.icmpSent == 0
            && hdr.ipv4.srcAddr != VIP_ADDR && hdr.ipv4.dstAddr != VIP_ADDR) { intWatchlist.apply(); }
        intMtuConfig.apply();
        if (meta.intSource == 1 && meta.watched == 1 && !hdr.int_shim.isValid()
            && hdr.ipv4.fragOffset == 0 && hdr.ipv4.flags[0:0] == 0
            && hdr.ipv4.ihl == 5 && (hdr.tcp.isValid() || hdr.udp.isValid())) {
            // Reserve the source, spine and sink metadata (3 * 16 bytes),
            // UDP (8), shim (4) and MD header (12) at the domain boundary.
            if (meta.intMtu == 0 || standard_metadata.packet_length + 72 <= (bit<32>)meta.intMtu) {
                hdr.int_udp.setValid(); hdr.int_udp.srcPort = meta.srcPort;
                hdr.int_udp.dstPort = INT_UDP_PORT; hdr.int_udp.checksum = 0;
                hdr.int_udp.length = hdr.ipv4.totalLen - 20 + 24;
                hdr.int_shim.setValid(); hdr.int_shim.type = 1;
                hdr.int_shim.nextProtoType = 2; hdr.int_shim.reserved = 0;
                hdr.int_shim.length = 3; hdr.int_shim.reserved2 = 0;
                hdr.int_shim.originalProtocol = hdr.ipv4.protocol;
                hdr.int_md.setValid(); hdr.int_md.version = 2;
                hdr.int_md.d = 0; hdr.int_md.e = 0; hdr.int_md.m = 0;
                hdr.int_md.reserved = 0; hdr.int_md.hopMl = 4;
                hdr.int_md.remaining = 8; hdr.int_md.instructions = 0xf000;
                hdr.int_md.domain = 0; hdr.int_md.dsInstructions = 0; hdr.int_md.dsFlags = 0;
                hdr.ipv4.protocol = PROTO_UDP;
                hdr.ipv4.totalLen = hdr.ipv4.totalLen + 24;
            }
        }
        // Apply this terminal ACL after routing: ipv4Lpm.forward otherwise
        // overwrites the egress drop set by icmpFragNeededDrop.
        if (meta.role == ROLE_GATEWAY && hdr.icmp.isValid()) { icmpAcl.apply(); }
        // I2E clones reparse the received packet before ingress modifications.
        if (hdr.int_md.isValid() && hdr.int_shim.length > 3) {
            clone(CloneType.I2E, REPORT_SESSION);
        }
    }
}

control GatewayEgress(inout headers hdr, inout metadata meta,
                      inout standard_metadata_t standard_metadata) {
    @name("egress_port_counter") counter(64, CounterType.packets_and_bytes) egressCounter;
    @name("queue_occupancy") register<bit<32>>(64) queueOccupancy;
    register<bit<22>>(1) reportSequence;
    @name("int_sink") action intSink() { meta.intSink = 1; }
    @name("int_sink_port") table intSinkPort {
        key = { standard_metadata.egress_port: exact; }
        actions = { intSink; NoAction; } size = 64; default_action = NoAction();
    }
    @name("set_int_node") action setIntNode(bit<32> switch_id, ipv4_t report_src, mac_t collector_mac) {
        meta.switchId = switch_id; meta.reportSrc = report_src; meta.collectorMac = collector_mac;
    }
    @name("int_node_config") table intNodeConfig {
        actions = { setIntNode; } default_action = setIntNode(0, 0, 0);
    }
    @name("set_ecn_threshold") action setEcnThreshold(bit<32> threshold) { meta.ecnThreshold = threshold; }
    @name("ecn_config") table ecnConfig {
        key = { standard_metadata.egress_port: exact; }
        actions = { setEcnThreshold; NoAction; }
        size = 64;
        default_action = NoAction();
    }
    apply {
        meta.intSink = 0; intSinkPort.apply();
        intNodeConfig.apply();
        if (standard_metadata.instance_type == 1 || standard_metadata.instance_type == 2) {
            if (standard_metadata.instance_type == 2) {
                // E2E clones retain the complete header stack after the sink's
                // egress pipeline, including the queue measurements of the data copy.
                hdr.ipv4 = hdr.saved_ipv4;
                hdr.int_udp = hdr.saved_int_udp; hdr.int_shim = hdr.saved_int_shim;
                hdr.int_md = hdr.saved_int_md;
                hdr.hops[0] = hdr.saved_hops[0];
                hdr.hops[1] = hdr.saved_hops[1];
                hdr.hops[2] = hdr.saved_hops[2];
                hdr.hops[3] = hdr.saved_hops[3];
                hdr.hops[4] = hdr.saved_hops[4];
                hdr.hops[5] = hdr.saved_hops[5];
                hdr.hops[6] = hdr.saved_hops[6];
                hdr.hops[7] = hdr.saved_hops[7];
            }
            bit<22> sequence;
            reportSequence.read(sequence, 0); reportSequence.write(0, sequence + 1);
            bit<16> innerLength = 20 + 8 + 4 + (bit<16>)hdr.int_shim.length * 4;
            if (hdr.tcp.isValid()) { innerLength = innerLength + 20; }
            else { innerLength = innerLength + 8; }
            hdr.report_ethernet.setValid();
            hdr.report_ethernet.dstAddr = meta.collectorMac;
            hdr.report_ethernet.srcAddr = hdr.ethernet.srcAddr;
            hdr.report_ethernet.etherType = TYPE_IPV4;
            hdr.ethernet.setInvalid();
            hdr.report_ipv4 = hdr.ipv4;
            hdr.report_ipv4.srcAddr = meta.reportSrc; hdr.report_ipv4.dstAddr = 0xac1d00fa;
            hdr.report_ipv4.diffserv = 0; hdr.report_ipv4.ihl = 5;
            hdr.report_ipv4.totalLen = 40 + innerLength;
            hdr.report_ipv4.flags = 0; hdr.report_ipv4.fragOffset = 0;
            hdr.report_ipv4.ttl = 64; hdr.report_ipv4.protocol = PROTO_UDP;
            hdr.report_udp.setValid(); hdr.report_udp.srcPort = 32766;
            hdr.report_udp.dstPort = 32766; hdr.report_udp.length = 20 + innerLength;
            hdr.report_udp.checksum = 0;
            hdr.report_group.setValid(); hdr.report_group.version = 2;
            hdr.report_group.hwId = (bit<6>)standard_metadata.instance_type; hdr.report_group.sequence = sequence;
            hdr.report_group.nodeId = meta.switchId;
            hdr.report.setValid(); hdr.report.repType = 0; hdr.report.inType = 4;
            hdr.report.length = (bit<8>)(innerLength >> 2); hdr.report.mdLength = 0;
            hdr.report.d = 0; hdr.report.q = 0; hdr.report.f = 1;
            hdr.report.i = 0; hdr.report.reserved = 0;
            truncate((bit<32>)(54 + innerLength));
        } else {
            egressCounter.count((bit<32>)standard_metadata.egress_port);
            bit<32> depth = (bit<32>)standard_metadata.deq_qdepth;
            queueOccupancy.write((bit<32>)standard_metadata.egress_port, depth);
            meta.ecnThreshold = 0; ecnConfig.apply();
            if (meta.ecnThreshold > 0 && depth >= meta.ecnThreshold &&
                (hdr.ipv4.diffserv[1:0] == 1 || hdr.ipv4.diffserv[1:0] == 2)) {
                hdr.ipv4.diffserv[1:0] = 3;
            }
            if (hdr.int_md.isValid()) {
                if (hdr.int_md.remaining == 0) { hdr.int_md.e = 1; }
                else if (hdr.ipv4.totalLen + 14 + 16 > meta.intMtu && meta.intMtu != 0) {
                    hdr.int_md.m = 1;
                } else {
                    hdr.hops.push_front(1); hdr.hops[0].setValid();
                    hdr.hops[0].switchId = meta.switchId;
                    hdr.hops[0].ingressPort = (bit<16>)standard_metadata.ingress_port;
                    hdr.hops[0].egressPort = (bit<16>)standard_metadata.egress_port;
                    hdr.hops[0].latency = (bit<32>)(standard_metadata.egress_global_timestamp
                        - standard_metadata.ingress_global_timestamp);
                    hdr.hops[0].queueId = 0;
                    hdr.hops[0].queueDepth = (bit<24>)standard_metadata.deq_qdepth;
                    hdr.int_md.remaining = hdr.int_md.remaining - 1;
                    hdr.int_shim.length = hdr.int_shim.length + 4;
                    hdr.ipv4.totalLen = hdr.ipv4.totalLen + 16;
                    hdr.int_udp.length = hdr.int_udp.length + 16;
                }
                {
                    // Snapshot each egress observation before physical transmission.
                    // E2E cloning copies headers at the end of egress.
                    hdr.saved_ipv4 = hdr.ipv4;
                    hdr.saved_int_udp = hdr.int_udp; hdr.saved_int_shim = hdr.int_shim;
                    hdr.saved_int_md = hdr.int_md;
                    hdr.saved_hops[0] = hdr.hops[0];
                    hdr.saved_hops[1] = hdr.hops[1];
                    hdr.saved_hops[2] = hdr.hops[2];
                    hdr.saved_hops[3] = hdr.hops[3];
                    hdr.saved_hops[4] = hdr.hops[4];
                    hdr.saved_hops[5] = hdr.hops[5];
                    hdr.saved_hops[6] = hdr.hops[6];
                    hdr.saved_hops[7] = hdr.hops[7];
                    clone(CloneType.E2E, REPORT_SESSION);
                }
                if (meta.intSink == 1) {
                    hdr.ipv4.protocol = hdr.int_shim.originalProtocol;
                    hdr.ipv4.totalLen = hdr.ipv4.totalLen
                        - 12 - (bit<16>)hdr.int_shim.length * 4;
                    hdr.int_udp.setInvalid(); hdr.int_shim.setInvalid();
                    hdr.int_md.setInvalid();
                    hdr.hops[0].setInvalid();
                    hdr.hops[1].setInvalid();
                    hdr.hops[2].setInvalid();
                    hdr.hops[3].setInvalid();
                    hdr.hops[4].setInvalid();
                    hdr.hops[5].setInvalid();
                    hdr.hops[6].setInvalid();
                    hdr.hops[7].setInvalid();
                    if (hdr.saved_int_md.d == 1) { mark_to_drop(standard_metadata); }
                }
            }
        }
    }
}
control Compute(inout headers hdr, inout metadata meta) {
    apply {
        update_checksum(hdr.ipv4.isValid(),
        {hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.diffserv, hdr.ipv4.totalLen,
         hdr.ipv4.identification, hdr.ipv4.flags, hdr.ipv4.fragOffset,
         hdr.ipv4.ttl, hdr.ipv4.protocol, hdr.ipv4.srcAddr, hdr.ipv4.dstAddr},
        hdr.ipv4.hdrChecksum, HashAlgorithm.csum16);
        update_checksum(hdr.report_ipv4.isValid(),
        {hdr.report_ipv4.version, hdr.report_ipv4.ihl, hdr.report_ipv4.diffserv, hdr.report_ipv4.totalLen,
         hdr.report_ipv4.identification, hdr.report_ipv4.flags, hdr.report_ipv4.fragOffset,
         hdr.report_ipv4.ttl, hdr.report_ipv4.protocol, hdr.report_ipv4.srcAddr, hdr.report_ipv4.dstAddr},
        hdr.report_ipv4.hdrChecksum, HashAlgorithm.csum16);
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
    apply {
        packet.emit(hdr.report_ethernet); packet.emit(hdr.report_ipv4);
        packet.emit(hdr.report_udp); packet.emit(hdr.report_group); packet.emit(hdr.report);
        packet.emit(hdr.ethernet); packet.emit(hdr.ipv4); packet.emit(hdr.int_udp);
        packet.emit(hdr.int_shim); packet.emit(hdr.int_md); packet.emit(hdr.hops);
        packet.emit(hdr.tcp); packet.emit(hdr.udp); packet.emit(hdr.icmp);
        packet.emit(hdr.icmp_quote_ipv4); packet.emit(hdr.icmp_quote_l4);
    }
}
V1Switch(GatewayParser(), Verify(), GatewayIngress(), GatewayEgress(), Compute(), GatewayDeparser()) main;
