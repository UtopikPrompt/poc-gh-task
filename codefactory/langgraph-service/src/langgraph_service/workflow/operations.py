"""Bounded operations supplied to the task lifecycle graph."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

Operation = Callable[[Mapping[str, object]], Mapping[str, object]]


@dataclass(frozen=True)
class WorkflowOperations:
    gather_context: Operation | None = None
    create_plan: Operation | None = None
    assess_risk: Operation | None = None
    prepare_changes: Operation | None = None
    repair_changes: Operation | None = None
    request_human_feedback: Operation | None = None
