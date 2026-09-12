"""Read-only integration/release gates. Broken graphs and missing evidence fail closed.

Approval is a workflow attestation, not an OS permission boundary between root
processes. Release completion additionally requires its own deployment receipt.
"""
from __future__ import annotations

from typing import Any
from pathlib import Path

from .state import DONE, HiveError, state_directory, validate_task
from .completion import completion


def validate(task: dict[str, Any], *, phase: str = "integrate", state_dir: Path | None = None) -> dict[str, Any]:
    if phase not in {"integrate", "release", "complete"}:
        raise HiveError("phase must be integrate, release or complete")
    validate_task(task)
    if "workplan" in task:
        unfinished = [p["id"] for p in task["workplan"]["packages"] if p["status"] != "succeeded"]
        if unfinished:
            raise HiveError("gate blocked by incomplete work packages: " + ",".join(unfinished))
    nodes = {node["id"]: node for node in task["nodes"]}
    analysis = task.get("kind") == "analysis"
    if analysis and phase != "complete":
        raise HiveError("analysis tasks cannot integrate or release; explicit workflow promotion required")
    required = [node for node in task["nodes"] if node["id"] not in {"integrate", "release"}]
    if not analysis and phase in {"release", "complete"}:
        required.append(nodes["integrate"])
    if not analysis and phase == "complete":
        required.append(nodes["release"])
    for node in required:
        if node["status"] not in DONE:
            raise HiveError("gate blocked by incomplete node: " + node["id"])
        if not node["artifacts"]:
            raise HiveError(f"gate requires {node['id']} evidence artifact")
        if node["id"] in {"integrate", "release"} and not node.get("approved_at"):
            raise HiveError("gate requires recorded approval: " + node["id"])
    author = nodes["plan" if analysis else "implement"]["owner"]
    if author.strip().casefold() == nodes["review"]["owner"].strip().casefold():
        raise HiveError("reviewer must differ from implementer")
    if not analysis and task.get("size") == "L":
        security_owner = nodes["security"]["owner"]
        if security_owner.strip().casefold() == author.strip().casefold():
            raise HiveError("L security reviewer must differ from implementer")
    if phase == "complete" and task.get("status") != "complete":
        raise HiveError("task is not complete")
    if phase == "complete":
        complete, missing = completion(task, directory=state_directory(state_dir))
        if not complete:
            raise HiveError("factual completion requires " + "; ".join(missing))
    return {"task_id": task["id"], "revision": task["revision"], "phase": phase, "ready": True}
