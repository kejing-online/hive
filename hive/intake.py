"""Stable Hive identities for factory-originated development work."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .state import HiveError, _read, _save, create_task, locked_task, state_directory


DEFAULT_REPO = (__import__("os").environ.get("HIVE_DEFAULT_REPO") or "").strip()


def canonical_source(repo: str, source_key: str) -> str:
    if not isinstance(repo, str) or not repo.strip():
        raise HiveError("repo is required")
    if not isinstance(source_key, str) or not source_key.strip():
        raise HiveError("source_key is required")
    return repo.strip() + "::" + source_key.strip()


def ensure_task(goal: str, *, repo: str, source_key: str, size: str = "M",
                kind: str = "development", state_dir: Path | None = None) -> dict[str, Any]:
    """Create or reuse one identity; completed tasks never silently reopen."""
    repo = repo.strip()
    source_key = source_key.strip()
    kind = str(kind or "").strip().lower()
    if kind not in {"development", "analysis"}:
        raise HiveError("kind must be development or analysis")
    canonical = canonical_source(repo, source_key)
    task = create_task(goal, size=size, kind=kind, state_dir=state_dir, source_key=canonical)
    with locked_task(task["id"], state_dir) as directory:
        task = _read(task["id"], directory)
        if task.get("kind") != kind:
            raise HiveError("source_key already has a different task kind; explicit workflow promotion required")
        context = task.setdefault("context", {})
        existing = (context.get("repo"), context.get("source_key"))
        wanted = (repo, source_key)
        if existing != (None, None) and existing != wanted:
            raise HiveError("Hive task context does not match its canonical source")
        if existing != wanted:
            context.update(repo=repo, source_key=source_key)
            _save(task, directory)
        return task


def ensure_factory_task(ticket: dict[str, Any], home: Path) -> dict[str, Any]:
    """Attach a factory ticket to its durable Hive task, without advancing it."""
    if not isinstance(ticket, dict):
        raise HiveError("factory ticket must be an object")
    ticket_id = str(ticket.get("ticket_id") or "").strip()
    if not ticket_id:
        raise HiveError("factory ticket requires ticket_id")
    raw_source = ticket.get("source_ref")
    source_key = str(raw_source).strip() if isinstance(raw_source, str) and raw_source.strip() else "factory:" + ticket_id
    repo = str(ticket.get("repo") or DEFAULT_REPO).strip() or DEFAULT_REPO
    goal = str(ticket.get("title") or "").strip() or "factory ticket " + ticket_id
    size = str(ticket.get("hive_size") or ticket.get("size_hint") or "M").upper()
    if size not in {"S", "M", "L"}:
        size = "M"
    # The factory's configured shared state wins. Temporary factory homes keep
    # their Hive state local so tests and dry factories never touch production.
    directory = state_directory() if os.environ.get("HIVE_STATE_DIR") else state_directory(Path(home) / "hive")
    kind = str(ticket.get("hive_kind") or ("analysis" if str(ticket.get("tier") or "") in {"T0", "T3"} else "development")).strip().lower()
    task = ensure_task(goal, repo=repo, source_key=source_key, size=size, kind=kind, state_dir=directory)
    ticket["hive_task_id"] = task["id"]
    ticket["hive_state_dir"] = str(directory)
    ticket["hive_size"] = task["size"]
    ticket["hive_kind"] = task["kind"]
    return task
