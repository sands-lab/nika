"""Tests for compound benchmark task labels."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.task_label import (
    format_task_label,
    parse_task_label,
)


class TestFormatTaskLabel:
    def test_non_scalable(self) -> None:
        assert format_task_label("simple_bgp", "link_down") == "simple_bgp_link_down"
        assert (
            format_task_label("simple_bgp", "link_down", None) == "simple_bgp_link_down"
        )
        assert (
            format_task_label("simple_bgp", "link_down", "") == "simple_bgp_link_down"
        )

    def test_scalable(self) -> None:
        assert (
            format_task_label("dc_clos_bgp", "link_down", "s")
            == "dc_clos_bgp_s_link_down"
        )

    def test_rejects_missing_size_for_scalable(self) -> None:
        with pytest.raises(ValueError, match="requires topo_size"):
            format_task_label("dc_clos_bgp", "link_down")

    def test_rejects_size_for_non_scalable(self) -> None:
        with pytest.raises(ValueError, match="does not use sizes"):
            format_task_label("simple_bgp", "link_down", "s")


class TestParseTaskLabel:
    def test_round_trip_non_scalable(self) -> None:
        label = format_task_label("simple_bgp", "link_down")
        assert parse_task_label(label) == ("simple_bgp", "", "link_down")

    def test_round_trip_scalable(self) -> None:
        label = format_task_label("dc_clos_bgp", "link_down", "s")
        assert parse_task_label(label) == ("dc_clos_bgp", "s", "link_down")

    def test_unknown_label(self) -> None:
        with pytest.raises(ValueError, match="Unknown task label"):
            parse_task_label("not_a_real_task_label_xyz")

    def test_empty_label(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_task_label("  ")

    def test_missing_size_for_scalable_is_unknown(self) -> None:
        with pytest.raises(ValueError, match="Unknown task label"):
            parse_task_label("dc_clos_bgp_link_down")

    def test_extra_size_for_non_scalable_is_unknown(self) -> None:
        with pytest.raises(ValueError, match="Unknown task label"):
            parse_task_label("simple_bgp_s_link_down")
