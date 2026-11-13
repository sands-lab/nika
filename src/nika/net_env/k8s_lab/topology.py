"""Size-dependent Kubernetes attachments to the two-pod fabric."""

from dataclasses import dataclass


@dataclass(frozen=True)
class K8sTopology:
    size: str = "m"

    def __post_init__(self):
        if self.size not in {"s", "m", "l"}:
            raise ValueError("Topology size must be one of: s, m, l.")

    @property
    def workers(self) -> tuple[str, ...]:
        return tuple(
            f"worker{i}" for i in range(1, {"s": 3, "m": 5, "l": 8}[self.size] + 1)
        )

    @property
    def leaf_nodes(self) -> dict[str, tuple[str, ...]]:
        split = len(self.workers) // 2
        return {
            "leaf_1_1": ("controller", *self.workers[:split]),
            "leaf_1_2": self.workers[split:],
        }

    @property
    def addresses(self) -> dict[str, str]:
        return {
            node: f"201.{leaf}.{port}.2"
            for leaf, nodes in enumerate(self.leaf_nodes.values(), 1)
            for port, node in enumerate(nodes, 1)
        }

    @property
    def storage_nodes(self) -> tuple[str, str]:
        return ("worker1", self.leaf_nodes["leaf_1_2"][1])

    @property
    def app_replicas(self) -> int:
        return {"s": 1, "m": 2, "l": 3}[self.size]
