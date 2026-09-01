/* -*- P4_16 -*- */
#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x800;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

struct metadata { }
struct headers {
    ethernet_t ethernet;
    ipv4_t     ipv4;
}

parser BlackholeParser(packet_in packet,
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
        transition accept;
    }
}

control BlackholeVerifyChecksum(inout headers hdr, inout metadata meta) {
    apply { }
}

control BlackholeIngress(inout headers hdr,
                         inout metadata meta,
                         inout standard_metadata_t standard_metadata) {
    @name("drop")
    action drop() {
        mark_to_drop(standard_metadata);
    }

    @name("drop_all")
    table drop_all {
        key = {
            hdr.ipv4.dstAddr: lpm;
        }
        actions = {
            drop;
        }
        size = 8;
        default_action = drop();
    }

    apply {
        drop_all.apply();
    }
}

control BlackholeEgress(inout headers hdr,
                        inout metadata meta,
                        inout standard_metadata_t standard_metadata) {
    apply { }
}

control BlackholeComputeChecksum(inout headers hdr, inout metadata meta) {
    apply { }
}

control BlackholeDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
    }
}

V1Switch(
    BlackholeParser(),
    BlackholeVerifyChecksum(),
    BlackholeIngress(),
    BlackholeEgress(),
    BlackholeComputeChecksum(),
    BlackholeDeparser()
) main;
