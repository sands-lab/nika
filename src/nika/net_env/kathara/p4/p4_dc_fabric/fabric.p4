/* -*- P4_16 -*- */
#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x800;
const bit<8>  PROTO_TCP = 6;
const bit<8>  PROTO_UDP = 17;

#define IPV4_LPM_SIZE 256
#define ECMP_MEMBER_SIZE 256
#define ECMP_GROUP_SIZE 128
#define PORT_COUNT 32

typedef bit<9>  egressSpec_t;
typedef bit<48> macAddr_t;
typedef bit<32> ip4Addr_t;

header ethernet_t {
    macAddr_t dstAddr;
    macAddr_t srcAddr;
    bit<16>   etherType;
}

header ipv4_t {
    bit<4>    version;
    bit<4>    ihl;
    bit<8>    diffserv;
    bit<16>   totalLen;
    bit<16>   identification;
    bit<3>    flags;
    bit<13>   fragOffset;
    bit<8>    ttl;
    bit<8>    protocol;
    bit<16>   hdrChecksum;
    ip4Addr_t srcAddr;
    ip4Addr_t dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
}

struct metadata {
    bit<16> l4_srcPort;
    bit<16> l4_dstPort;
}

struct headers {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
    udp_t      udp;
}

parser FabricParser(packet_in packet,
                    out headers hdr,
                    inout metadata meta,
                    inout standard_metadata_t standard_metadata) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            TYPE_IPV4: parse_ipv4;
            default: accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            PROTO_TCP: parse_tcp;
            PROTO_UDP: parse_udp;
            default: accept;
        }
    }

    state parse_tcp {
        packet.extract(hdr.tcp);
        transition accept;
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition accept;
    }
}

control FabricVerifyChecksum(inout headers hdr, inout metadata meta) {
    apply { }
}

control FabricIngress(inout headers hdr,
                      inout metadata meta,
                      inout standard_metadata_t standard_metadata) {

    @name("ingress_port_counter")
    counter(PORT_COUNT, CounterType.packets) ingress_port_counter;

    @name("ecmp_selector")
    action_selector(HashAlgorithm.crc16, 32w256, 32w16) ecmp_selector;

    @name("drop")
    action drop() {
        mark_to_drop(standard_metadata);
    }

    @name("ipv4_forward")
    action ipv4_forward(macAddr_t srcAddr, macAddr_t dstAddr, egressSpec_t port) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = srcAddr;
        hdr.ethernet.dstAddr = dstAddr;
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
    }

    @name("ipv4_lpm")
    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr: lpm;
            hdr.ipv4.srcAddr: selector;
            hdr.ipv4.protocol: selector;
            meta.l4_srcPort: selector;
            meta.l4_dstPort: selector;
        }
        actions = {
            ipv4_forward;
            drop;
        }
        implementation = ecmp_selector;
        size = IPV4_LPM_SIZE;
    }

    apply {
        ingress_port_counter.count((bit<32>)standard_metadata.ingress_port);
        if (!hdr.ipv4.isValid()) {
            drop();
            return;
        }
        if (hdr.ipv4.ttl <= 1) {
            drop();
            return;
        }
        meta.l4_srcPort = 0;
        meta.l4_dstPort = 0;
        if (hdr.tcp.isValid()) {
            meta.l4_srcPort = hdr.tcp.srcPort;
            meta.l4_dstPort = hdr.tcp.dstPort;
        } else if (hdr.udp.isValid()) {
            meta.l4_srcPort = hdr.udp.srcPort;
            meta.l4_dstPort = hdr.udp.dstPort;
        }
        ipv4_lpm.apply();
    }
}

control FabricEgress(inout headers hdr,
                     inout metadata meta,
                     inout standard_metadata_t standard_metadata) {
    @name("egress_port_counter")
    counter(PORT_COUNT, CounterType.packets) egress_port_counter;

    apply {
        egress_port_counter.count((bit<32>)standard_metadata.egress_port);
    }
}

control FabricComputeChecksum(inout headers hdr, inout metadata meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version,
              hdr.ipv4.ihl,
              hdr.ipv4.diffserv,
              hdr.ipv4.totalLen,
              hdr.ipv4.identification,
              hdr.ipv4.flags,
              hdr.ipv4.fragOffset,
              hdr.ipv4.ttl,
              hdr.ipv4.protocol,
              hdr.ipv4.srcAddr,
              hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

control FabricDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.tcp);
        packet.emit(hdr.udp);
    }
}

V1Switch(
    FabricParser(),
    FabricVerifyChecksum(),
    FabricIngress(),
    FabricEgress(),
    FabricComputeChecksum(),
    FabricDeparser()
) main;
