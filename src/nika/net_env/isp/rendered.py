"""Stored netlab configurations for ISP deployments.

Each ISP scenario stores one variant per IGP, BGP mode and device profile
that the benchmark uses, under ``isp/rendered/<scenario>/<variant>``. A
deployment without stored configurations, such as an ad-hoc topology, keeps
NIKA's rendering.

IOS XR and RouterOS ISP deployments keep NIKA's rendering: their runtime
operations, telemetry and fault bindings address the object names that NIKA
renders, such as the RouterOS ``lo0`` loopback, which netlab names differently.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from nika.config import BENCHMARK_DIR
from nika.net_env.isp.igp.config import (
    DEFAULT_CONSTANT_METRIC,
    DEFAULT_METRIC_STRATEGY,
)

# Deployment options that select a stored variant.
VARIANT_OPTIONS = ("igp", "bgp_mode", "rpki", "rtbh", "device_profile", "backend")
# Device profiles whose ISP deployments run the stored netlab configurations.
RENDERED_PROFILES = frozenset({"frr", "nokia_srlinux"})


def benchmark_variants(scenario: str) -> list[dict]:
    """Deployment options of the scenario's cases in the pool and current release."""
    from nika.config import BENCHMARK_VERSION

    cases = []
    for path in sorted((BENCHMARK_DIR / "working" / "pool" / scenario).glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        for section in ("failure", "healthy"):
            cases += (data.get(section) or {}).get("cases", [])
    release = BENCHMARK_DIR / "releases" / BENCHMARK_VERSION
    for split in ("dev", "test"):
        path = release / f"{split}.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text())
            cases += [c for c in data["cases"] if c["scenario"] == scenario]
    variants = []
    for case in cases:
        options = {k: case[k] for k in VARIANT_OPTIONS if k in case}
        if options.get("device_profile", "frr") not in RENDERED_PROFILES:
            continue
        if options not in variants:
            variants.append(options)
    return variants


class IspRendered:
    """Rendered-configuration hooks shared by the ISP environment classes."""

    STORES_RENDERED_CONFIGS = True

    @classmethod
    def rendered_variants(cls, scenario: str) -> list[dict]:
        return benchmark_variants(scenario)

    def rendered_parts(self) -> tuple[str, ...]:
        parts = [self.plan.igp, self.bgp_mode, self.device_profile]
        parts += [name for name in ("rpki", "rtbh") if getattr(self, name, False)]
        if (self.metric_strategy, self.constant_metric) != (
            DEFAULT_METRIC_STRATEGY,
            DEFAULT_CONSTANT_METRIC,
        ):
            parts.append(f"{self.metric_strategy}{self.constant_metric}")
        return ("isp", "rendered", self.scenario_id, "-".join(parts))

    def _netlab_topology(self) -> dict | None:
        """The netlab input, or None when netlab cannot generate this deployment."""
        if "_netlab_input" not in self.__dict__:
            from nika.net_env.isp.netlab import netlab_topology

            try:
                topology = netlab_topology(
                    self.plan, self.bgp_plan, self.device_profile
                )
            except ValueError:
                topology = None
            self._netlab_input = topology
        return self._netlab_input

    def netlab_renderable(self) -> bool:
        return (
            self.device_profile in RENDERED_PROFILES
            and self._netlab_topology() is not None
        )

    def netlab_render_input(self) -> dict:
        # The plans also feed the steps that complete netlab's output, such
        # as the RPKI/RTBH overlay and iBGP policy attachment.
        payload = {
            "topology": self._netlab_topology(),
            "plan": repr(self.plan),
            "bgp": repr(self.bgp_plan),
        }
        if self.backend == "containerlab":
            # SR Linux keeps attributes of NIKA's documents netlab does not emit.
            payload["retained"] = self.native_routing_documents()
        return payload

    def render_netlab_configs(self, directory: Path) -> dict[str, str]:
        from nika.net_env.isp.netlab import generate_isp_configs

        return generate_isp_configs(self, directory)
