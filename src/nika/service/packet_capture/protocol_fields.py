"""Normalize tshark JSON into troubleshooting-friendly protocol fields."""

from __future__ import annotations

from typing import Any


def _layer(packet: dict[str, Any], name: str) -> dict[str, Any] | None:
    layers = packet.get("_source", {}).get("layers", {})
    layer = layers.get(name)
    return layer if isinstance(layer, dict) else None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def extract_protocol_fields(protocol: str, packet: dict[str, Any]) -> dict[str, Any]:
    protocol = protocol.lower().strip()
    layers = packet.get("_source", {}).get("layers", {})
    frame = _layer(packet, "frame") or {}
    base = {
        "frame_number": _first(frame.get("frame.number")),
        "frame_time": _first(frame.get("frame.time")),
    }

    if protocol in {"ethernet", "eth"}:
        eth = _layer(packet, "eth") or {}
        return {
            **base,
            "src_mac": _first(eth.get("eth.src")),
            "dst_mac": _first(eth.get("eth.dst")),
            "eth_type": _first(eth.get("eth.type")),
        }

    if protocol == "arp":
        arp = _layer(packet, "arp") or {}
        return {
            **base,
            "opcode": _first(arp.get("arp.opcode")),
            "src_mac": _first(arp.get("arp.src.hw_mac")),
            "src_ip": _first(arp.get("arp.src.proto_ipv4")),
            "dst_mac": _first(arp.get("arp.dst.hw_mac")),
            "dst_ip": _first(arp.get("arp.dst.proto_ipv4")),
        }

    if protocol in {"ip", "ipv4"}:
        ip = _layer(packet, "ip") or {}
        return {
            **base,
            "src": _first(ip.get("ip.src")),
            "dst": _first(ip.get("ip.dst")),
            "ttl": _first(ip.get("ip.ttl")),
            "proto": _first(ip.get("ip.proto")),
            "flags": _first(ip.get("ip.flags")),
        }

    if protocol in {"ipv6", "ip6"}:
        ip6 = _layer(packet, "ipv6") or {}
        return {
            **base,
            "src": _first(ip6.get("ipv6.src")),
            "dst": _first(ip6.get("ipv6.dst")),
            "hlim": _first(ip6.get("ipv6.hlim")),
            "nxt": _first(ip6.get("ipv6.nxt")),
        }

    if protocol == "icmp":
        icmp = _layer(packet, "icmp") or {}
        return {
            **base,
            "type": _first(icmp.get("icmp.type")),
            "code": _first(icmp.get("icmp.code")),
            "id": _first(icmp.get("icmp.ident")),
            "seq": _first(icmp.get("icmp.seq")),
        }

    if protocol == "tcp":
        tcp = _layer(packet, "tcp") or {}
        return {
            **base,
            "src_port": _first(tcp.get("tcp.srcport")),
            "dst_port": _first(tcp.get("tcp.dstport")),
            "flags": _first(tcp.get("tcp.flags")),
            "seq": _first(tcp.get("tcp.seq")),
            "ack": _first(tcp.get("tcp.ack")),
            "window": _first(tcp.get("tcp.window_size_value")),
            "analysis": _first(tcp.get("tcp.analysis.flags")),
        }

    if protocol == "udp":
        udp = _layer(packet, "udp") or {}
        return {
            **base,
            "src_port": _first(udp.get("udp.srcport")),
            "dst_port": _first(udp.get("udp.dstport")),
            "length": _first(udp.get("udp.length")),
        }

    if protocol == "dns":
        dns = _layer(packet, "dns") or {}
        return {
            **base,
            "qname": _first(dns.get("dns.qry.name")),
            "qtype": _first(dns.get("dns.qry.type")),
            "rcode": _first(dns.get("dns.flags.rcode")),
            "answers": _first(dns.get("dns.count.answers")),
            "response": _first(dns.get("dns.flags.response")),
        }

    if protocol == "dhcp":
        dhcp = _layer(packet, "dhcp") or {}
        return {
            **base,
            "msg_type": _first(dhcp.get("dhcp.option.dhcp")),
            "xid": _first(dhcp.get("dhcp.id")),
            "requested_ip": _first(dhcp.get("dhcp.option.requested_ip_address")),
            "client_mac": _first(dhcp.get("dhcp.hw.mac_addr")),
        }

    if protocol == "bgp":
        bgp = _layer(packet, "bgp") or {}
        return {
            **base,
            "type": _first(bgp.get("bgp.type")),
            "asn": _first(bgp.get("bgp.as")),
            "hold_time": _first(bgp.get("bgp.hold_time")),
            "router_id": _first(bgp.get("bgp.identifier")),
            "capabilities": _first(bgp.get("bgp.capability")),
        }

    if protocol == "ospf":
        ospf = _layer(packet, "ospf") or {}
        return {
            **base,
            "msg_type": _first(ospf.get("ospf.msgtype")),
            "area": _first(ospf.get("ospf.area")),
            "router_id": _first(ospf.get("ospf.rid")),
            "hello_interval": _first(ospf.get("ospf.helloint")),
            "dead_interval": _first(ospf.get("ospf.deadint")),
            "neighbor": _first(ospf.get("ospf.neighbor")),
        }

    if protocol == "http":
        http = _layer(packet, "http") or {}
        return {
            **base,
            "method": _first(http.get("http.request.method")),
            "host": _first(http.get("http.host")),
            "uri": _first(http.get("http.request.uri")),
            "status": _first(http.get("http.response.code")),
            "content_type": _first(http.get("http.content_type")),
        }

    if protocol in {"tls", "ssl"}:
        tls = _layer(packet, "tls") or _layer(packet, "ssl") or {}
        return {
            **base,
            "version": _first(tls.get("tls.handshake.version"))
            or _first(tls.get("ssl.handshake.version")),
            "sni": _first(tls.get("tls.handshake.extensions_server_name"))
            or _first(tls.get("ssl.handshake.extensions_server_name")),
            "alert": _first(tls.get("tls.alert_message"))
            or _first(tls.get("ssl.alert_message")),
        }

    # Unknown protocol: return available layer names for agent drill-down.
    return {
        **base,
        "layers": sorted(layers.keys()),
    }
