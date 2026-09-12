"""Compatibility bridge: existing executors report to Hive, never bypass its gates."""
from __future__ import annotations

from pathlib import Path

from .state import HiveError, _mark, _save, attach_binding, load_task, locked_task, mark_node, retry_node

TEAM_NODES = {
    "planning": "plan",
    "delivery": "implement",
    "platform": "verify",
}


def _require_worker_node(node_id: str) -> None:
    if node_id in {"integrate", "release"}:
        raise HiveError("team workers cannot bind an integration or release gate")


def _require_binding(task_id: str, external_id: str, node_id: str, *, state_dir: Path) -> None:
    if not isinstance(external_id, str) or not external_id.strip():
        raise HiveError("team receipt requires an external task id")
    task = load_task(task_id, state_dir=state_dir)
    binding = {"system": "team", "external_id": external_id, "node_id": node_id}
    if binding not in task.get("bindings", []):
        raise HiveError("team receipt has no matching Hive binding")


def bind_team_task(task_id: str, external_id: str, node_id: str, *, state_dir: Path) -> None:
    if node_id == "workflow":
        from .workflow import bind
        return bind(task_id, external_id, state_dir=state_dir)
    _require_worker_node(node_id)
    attach_binding(task_id, system="team", external_id=external_id, node_id=node_id, state_dir=state_dir)


def claim_team_task(task_id: str, external_id: str, node_id: str, run_id: str, *, state_dir: Path) -> None:
    if node_id == "workflow":
        from .workflow import claim
        return claim(task_id, external_id, run_id, state_dir=state_dir)
    _require_worker_node(node_id)
    if not isinstance(run_id, str) or not run_id.strip():
        raise HiveError("team receipt requires a run id")
    _require_binding(task_id, external_id, node_id, state_dir=state_dir)
    mark_node(task_id, node_id, "leased", owner=f"team:{run_id}", state_dir=state_dir)


def settle_team_task(task_id: str, external_id: str, node_id: str, run_id: str,
                      status: str, artifact: str, *, state_dir: Path, handoff: dict | None = None) -> None:
    if node_id == "workflow":
        from .workflow import settle
        return settle(task_id, external_id, run_id, status, artifact, handoff=handoff, state_dir=state_dir)
    _require_worker_node(node_id)
    if not isinstance(run_id, str) or not run_id.strip():
        raise HiveError("team receipt requires a run id")
    _require_binding(task_id, external_id, node_id, state_dir=state_dir)
    mapped = "succeeded" if status in {"completed", "done"} else status
    if mapped not in {"succeeded", "failed", "blocked", "interrupted"}:
        mapped = "failed"
    if mapped == "succeeded" and not artifact.strip():
        raise HiveError("team completed receipt requires an evidence artifact")
    with locked_task(task_id, state_dir) as directory:
        task = load_task(task_id, state_dir=state_dir)
        node = next((item for item in task["nodes"] if item["id"] == node_id), None)
        owner = f"team:{run_id}"
        if node is None or node.get("owner") != owner:
            raise HiveError("team receipt does not own the leased Hive node")
        current = node.get("status")
        # A process can die after Hive accepted its terminal receipt but before its
        # local card acknowledgement is durable. Only that exact receipt may replay.
        if current in {"succeeded", "failed", "blocked", "interrupted"}:
            if current != mapped:
                raise HiveError("team receipt conflicts with an existing terminal Hive receipt")
            if artifact.strip() and artifact not in node.get("artifacts", []):
                raise HiveError("team receipt conflicts with existing terminal evidence")
            return
        if current not in {"leased", "running"}:
            raise HiveError("team receipt requires a leased or running Hive node")
        _mark(task, node_id, mapped, owner=owner, artifact=artifact, approved=False)
        _save(task, directory)


def retry_team_task(task_id: str, external_id: str, node_id: str, *, reason: str,
                     state_dir: Path) -> None:
    if node_id == "workflow":
        from .workflow import retry
        return retry(task_id, external_id, reason=reason, state_dir=state_dir)
    _require_worker_node(node_id)
    _require_binding(task_id, external_id, node_id, state_dir=state_dir)
    retry_node(task_id, node_id, owner=f"team:retry:{external_id}", reason=reason, state_dir=state_dir)
