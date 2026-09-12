"""Pure completion predicate shared by Hive state projections and gates.

This module deliberately does not import state, guard, or delivery.  Delivery
records facts; every consumer asks this predicate whether those facts prove a
development task is complete.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any


def completion(task: dict[str, Any], *, directory: Path | None = None) -> tuple[bool, list[str]]:
    """Return whether the task has a factual terminal result and missing facts."""
    # The helper is also used by read-only projections.  Validate there rather
    # than trusting a hand-built snapshot to have a real Hive DAG.
    try:
        from .state import validate_task
        validate_task(task)
    except (TypeError, ValueError, KeyError) as exc:
        return False, ["invalid Hive task: " + str(exc)]
    nodes = task.get("nodes") or []
    if "workplan" in task:
        unfinished = [p["id"] for p in task["workplan"]["packages"] if p["status"] != "succeeded"]
        if unfinished:
            return False, ["incomplete work package: " + item for item in unfinished]
    if task.get("kind") == "analysis":
        missing = [str(node.get("id")) for node in nodes if node.get("status") not in {"succeeded", "skipped"}]
        return not missing, ["incomplete node: " + node for node in missing]

    delivery = task.get("delivery")
    if not isinstance(delivery, dict):
        return False, ["missing registered delivery"]
    registered_sha = delivery.get("code_sha")
    integrated = delivery.get("integrated")
    released = delivery.get("released")
    required = delivery.get("required_components")
    if not isinstance(registered_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", registered_sha, re.IGNORECASE):
        return False, ["missing registered SHA"]
    if not isinstance(integrated, dict) or not integrated.get("main_sha"):
        return False, ["missing integration receipt"]
    if not isinstance(released, dict) or released.get("expected_sha") != integrated.get("main_sha") or released.get("observed_sha") != integrated.get("main_sha"):
        return False, ["missing matching release receipt"]
    if not isinstance(required, list) or not required:
        return False, ["missing required components"]
    component_receipts = delivery.get("component_receipts")
    if not isinstance(component_receipts, dict) or set(required) - set(component_receipts):
        return False, ["missing component receipt"]
    try:
        from .receipt_validation import validate_release_receipt
    except ImportError:
        return False, ["release receipt validator unavailable"]
    for component in required:
        receipt = component_receipts.get(component)
        try:
            validate_release_receipt(receipt, component, integrated["main_sha"], directory=directory)
        except (TypeError, ValueError, KeyError, OSError, AttributeError) as exc:
            return False, [f"invalid {component} release receipt: {exc}"]
    missing = [str(node.get("id")) for node in nodes if node.get("status") not in {"succeeded", "skipped"}]
    return not missing, ["incomplete node: " + node for node in missing]
