"""Framework-neutral episode requests backed by the normal benchmark runner.

A request binds a task instance, an agent configuration and an execution
backend. The result separates execution outcome from reward and points to
host-owned artifacts. Search, sampling and policy selection belong to callers.
Native and Firecracker requests use the same contract.
"""

from .api import RolloutRequest, RolloutResult, run_rollout

__all__ = ["RolloutRequest", "RolloutResult", "run_rollout"]
