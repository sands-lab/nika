from __future__ import annotations

from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.load_config import load_benchmark_input


def inject_params_from_benchmark_yaml(
    scenario: str,
    problem: str,
    topo_size: str = "",
) -> dict[str, str]:
    """Load inject parameters for a benchmark row from the working pool."""
    normalized_topo = topo_size or ""
    pool = BENCHMARK_DIR / "working" / "pool"
    if not pool.is_dir():
        raise ValueError(
            f"No benchmark inject entry for scenario={scenario!r}, problem={problem!r}, "
            f"topo_size={topo_size!r}; pass explicit inject parameters."
        )
    for row in load_benchmark_input(pool):
        if (
            row["scenario"] == scenario
            and row["problem"] == problem
            and (row.get("topo_size") or "") == normalized_topo
        ):
            return dict(row["inject"])
    raise ValueError(
        f"No benchmark inject entry for scenario={scenario!r}, problem={problem!r}, "
        f"topo_size={topo_size!r}; pass explicit inject parameters."
    )
