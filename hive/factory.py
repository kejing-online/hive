"""Project a factory ticket onto the Hive control plane.

This is the portable factory: pause file, queued tickets, source-key identity.
Host-specific budget/kernel/deploy gates stay in the host adapter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .intake import ensure_factory_task
from .state import HiveError, _read, _save, load_task, locked_task


def admission_reason(ticket: dict[str, Any], home: Path, *, launch_model: bool = False) -> str:
    if (Path(home) / "PAUSE").is_file():
        return "factory paused"
    admitted = bool(ticket.get("team_task_id") or ticket.get("hive_task_id"))
    allowed = {"queued", "hive_queued"} if admitted else {"queued"}
    if ticket.get("status") not in allowed or ticket.get("injection"):
        return "factory ticket is historical, live, or quarantined"
    if not str(ticket.get("ticket_id") or "").strip():
        return "factory ticket requires ticket_id"
    if not launch_model and str(ticket.get("require_model") or "").strip() == "1":
        return "model execution disabled"
    return ""


def admit(ticket: dict[str, Any], home: Path, *, launch_model: bool = False) -> dict[str, Any]:
    """Bind one queued ticket to one Hive task. Does not start workers."""
    if not isinstance(ticket, dict):
        raise HiveError("factory ticket must be an object")
    reason = admission_reason(ticket, home, launch_model=launch_model)
    if reason:
        raise HiveError(reason)
    task = ensure_factory_task(ticket, Path(home))
    state_dir = Path(str(ticket["hive_state_dir"]))
    allowed = ticket.get("allowed_paths")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(item, str) and item.strip() for item in allowed)
    ):
        raise HiveError("allowed_paths must be a list of strings")
    scope = {
        "tier": ticket.get("tier"),
        "slice": ticket.get("worker_slice"),
        "allowed_paths": allowed,
    }
    with locked_task(task["id"], state_dir) as directory:
        current = _read(task["id"], directory)
        existing = current.setdefault("context", {}).get("factory_scope")
        if existing and existing != scope:
            raise HiveError("same-source factory scope differs; explicit authorized scope migration required")
        if existing is None:
            current["context"]["factory_scope"] = scope
            _save(current, directory)
    ticket["hive_task_id"] = task["id"]
    ticket["hive_state_dir"] = str(state_dir)
    ticket["status"] = "hive_queued"
    return {
        "ticket_id": str(ticket["ticket_id"]),
        "hive_task_id": task["id"],
        "hive_state_dir": str(state_dir),
        "kind": task["kind"],
        "factory_scope": scope,
        "hive": {"task_id": task["id"], "node_id": "workflow"},
    }


def project(ticket: dict[str, Any], home: Path) -> dict[str, Any]:
    """Reload the bound Hive task. No retries and no node writes."""
    task_id = str(ticket.get("hive_task_id") or "")
    if not task_id:
        raise HiveError("factory ticket is not admitted")
    directory = Path(str(ticket.get("hive_state_dir") or Path(home) / "hive"))
    return load_task(task_id, state_dir=directory)
