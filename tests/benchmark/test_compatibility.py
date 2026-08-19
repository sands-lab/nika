"""Config-scoped failure × scenario compatibility for the coverage matrix."""

from __future__ import annotations

from nika.workflows.benchmark.compatibility import (
    compatible,
    compatible_columns,
    coverage_columns,
    effective_tags,
)


def test_coverage_columns_include_config_variants() -> None:
    cols = coverage_columns()
    assert "dc_clos/host" in cols
    assert "dc_clos/service" in cols
    assert "campus_lan/static" in cols
    assert "campus_lan/dhcp" in cols
    assert "isp/isis" in cols
    assert "isp/ospf" in cols
    assert "isp/ibgp_rr" in cols
    assert "isp/abilene-ebgp" in cols
    assert "simple_bgp" in cols


def test_link_down_compatible_with_all_columns() -> None:
    cols = coverage_columns()
    assert compatible_columns("link_down") == cols
    assert all(compatible("link_down", col) for col in cols)


def test_ospf_not_on_isp_isis() -> None:
    assert compatible("ospf_neighbor_missing", "campus_lan/static")
    assert compatible("ospf_neighbor_missing", "campus_lan/dhcp")
    assert compatible("ospf_neighbor_missing", "isp/ospf")
    assert not compatible("ospf_neighbor_missing", "isp/isis")
    assert not compatible("ospf_neighbor_missing", "isp/ibgp_rr")
    assert not compatible("ospf_neighbor_missing", "dc_clos/host")


def test_bgp_needs_bgp_enabled_isp() -> None:
    assert compatible("bgp_asn_misconfig", "dc_clos/host")
    assert compatible("bgp_asn_misconfig", "dc_clos/service")
    assert compatible("bgp_asn_misconfig", "isp/ibgp_rr")
    assert compatible("bgp_asn_misconfig", "isp/abilene-ebgp")
    assert not compatible("bgp_asn_misconfig", "isp/isis")
    assert not compatible("bgp_asn_misconfig", "isp/ospf")


def test_rpki_only_abilene_ebgp() -> None:
    cols = compatible_columns("bgp_rpki_invalid_route_leak")
    assert cols == ["isp/abilene-ebgp"]


def test_dhcp_not_on_campus_static() -> None:
    assert compatible("dhcp_service_down", "campus_lan/dhcp")
    assert not compatible("dhcp_service_down", "campus_lan/static")
    assert not compatible("dhcp_service_down", "dc_clos/host")


def test_dns_needs_service_or_campus_with_dns() -> None:
    assert compatible("dns_service_down", "dc_clos/service")
    assert compatible("dns_service_down", "campus_lan/dhcp")
    assert compatible("dns_service_down", "campus_lan/static")
    assert not compatible("dns_service_down", "dc_clos/host")


def test_http_not_on_clos_host() -> None:
    assert compatible("http_acl_block", "dc_clos/service")
    assert compatible("http_acl_block", "campus_lan/static")
    assert not compatible("http_acl_block", "dc_clos/host")


def test_dc_clos_host_omits_dns_http() -> None:
    tags = effective_tags("dc_clos/host")
    assert "bgp" in tags
    assert "dns" not in tags
    assert "http" not in tags
    service = effective_tags("dc_clos/service")
    assert {"dns", "http"}.issubset(service)
