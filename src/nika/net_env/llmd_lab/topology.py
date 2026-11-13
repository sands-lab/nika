"""Kubernetes nodes and simulator replicas on the inference LAN."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMDTopology:
    size: str = "m"

    def __post_init__(self):
        if self.size not in {"s", "m", "l"}:
            raise ValueError("Topology size must be one of: s, m, l.")

    @property
    def addresses(self) -> dict[str, str]:
        # Preserve the original controller, worker, client and web addresses.
        count = {"s": 3, "m": 5, "l": 8}[self.size]
        return {
            "controller": "200.0.0.1",
            **{
                f"worker{i}": f"200.0.0.{i + (1 if i <= 5 else 3)}"
                for i in range(1, count + 1)
            },
        }

    @property
    def replicas(self) -> tuple[int, int, int]:
        return {"s": (1, 2, 1), "m": (1, 3, 2), "l": (2, 5, 3)}[self.size]
