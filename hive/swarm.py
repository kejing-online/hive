"""Queen schedules; workers produce; soldiers gate.

Does not invent DAG stages, does not mark released, does not retry failed
packages, and does not default to an implicit executor.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import dispatch, executor, scent, workplan_runtime
from .intake import ensure_task
from .state import HiveError, load_task, mark_node, state_directory


SWARM_OWNER = "swarm"
DEFAULT_WRITE = ".hive-swarm-output"
_SKIP_NAMES = {
    ".git", ".hg", ".svn", ".hive", ".hive-state", ".venv", "venv",
    "node_modules", "__pycache__", ".tox", "dist", "build", ".eggs",
}
_SLUG = re.compile(r"[^a-zA-Z0-9]+")


def resolve_adapter(adapter: str | None, command_file: str | Path | None) -> str:
    name = (adapter or os.environ.get("HIVE_EXECUTOR_ADAPTER") or "").strip()
    if not name:
        raise HiveError("adapter is required; pass --adapter or set HIVE_EXECUTOR_ADAPTER")
    if name == "command" and not command_file:
        raise HiveError("command adapter requires --command-file")
    return name


def _slug(name: str) -> str:
    text = _SLUG.sub("-", name).strip("-").lower() or "slice"
    return text[:40]


def _role(package_id: str) -> str:
    if str(package_id).startswith("soldier-"):
        return "soldier"
    if str(package_id).startswith("worker-"):
        return "worker"
    return "worker"


def _scan_slices(repo: Path, *, limit: int = 6) -> list[str]:
    if not repo.is_dir():
        return [DEFAULT_WRITE]
    names = []
    try:
        entries = sorted(repo.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return [DEFAULT_WRITE]
    dirs = [p.name for p in entries if p.is_dir() and p.name not in _SKIP_NAMES and not p.name.startswith(".")]
    files = [p.name for p in entries if p.is_file() and p.name not in _SKIP_NAMES and not p.name.startswith(".")]
    names = dirs[:limit] or files[:limit]
    return names or [DEFAULT_WRITE]


def auto_plan(goal: str, *, repo: Path | None = None, write_paths: list[str] | None = None,
              max_workers: int = 6) -> dict[str, Any]:
    """Queen splits work: one worker per slice, then soldier-verify and soldier-review.

    Slices come from ``write_paths`` or top-level repo dirs/files. This does not
    let the model invent DAG stages.
    """
    if write_paths:
        slices = [p for p in write_paths if str(p).strip()]
    elif repo is not None:
        slices = _scan_slices(Path(repo), limit=max_workers)
    else:
        slices = [DEFAULT_WRITE]
    if not slices:
        slices = [DEFAULT_WRITE]
    workers = []
    seen = set()
    for raw in slices:
        ident = "worker-" + _slug(raw)
        if ident in seen:
            ident = ident + "-x"
        seen.add(ident)
        workers.append({
            "id": ident,
            "title": f"worker {raw}: {goal.strip()[:120]}",
            "depends_on": [],
            "read_paths": [],
            "write_paths": [raw],
            "acceptance": ["worker finished assigned write paths; not a release"],
            "priority": 60,
            "max_attempts": 2,
        })
    worker_ids = [row["id"] for row in workers]
    soldiers = [
        {
            "id": "soldier-verify",
            "title": "soldier verify",
            "depends_on": list(worker_ids),
            "read_paths": list(slices),
            "write_paths": [".hive-verify"],
            "acceptance": ["soldier verify; not a release"],
            "priority": 40,
            "max_attempts": 2,
        },
        {
            "id": "soldier-review",
            "title": "soldier review",
            "depends_on": ["soldier-verify"],
            "read_paths": [".hive-verify"],
            "write_paths": [".hive-review"],
            "acceptance": ["soldier review; reviewer is not the worker; not a release"],
            "priority": 30,
            "max_attempts": 2,
        },
    ]
    n_parallel = min(4, max(1, len(workers)))
    return {"max_parallel": n_parallel, "packages": workers + soldiers}


def default_plan(goal: str, *, write_paths: list[str] | None = None, repo: Path | None = None) -> dict[str, Any]:
    return auto_plan(goal, repo=repo, write_paths=write_paths)


def roster(task_id: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    directory = state_directory(state_dir)
    task = load_task(task_id, state_dir=directory)
    packages = []
    plan = task.get("workplan")
    if isinstance(plan, dict):
        for package in plan.get("packages") or []:
            lease = package.get("lease") or {}
            ident = package.get("id")
            packages.append({
                "package_id": ident,
                "role": _role(str(ident or "")),
                "status": package.get("status"),
                "owner": lease.get("owner") or package.get("owner"),
            })
    try:
        entries = dispatch.list_entries(task_id, state_dir=directory)
    except (HiveError, OSError, KeyError):
        entries = []
    return {
        "task_id": task_id,
        "task_status": task.get("status"),
        "goal": task.get("goal"),
        "queen": {"role": "queen", "owner": SWARM_OWNER, "job": "schedule"},
        "scent": scent.field(task),
        "packages": packages,
        "dispatches": [
            {"dispatch_id": row.get("id"), "status": row.get("status"),
             "package_id": row.get("package_id"), "role": _role(str(row.get("package_id") or ""))}
            for row in entries
        ],
    }


def _repo_label(repo: Path) -> str:
    try:
        raw = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            check=False, capture_output=True, text=True, timeout=10,
        ).stdout.strip().rstrip("/")
    except (OSError, subprocess.TimeoutExpired):
        raw = ""
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", raw)
    if match:
        return match.group(1)
    return "local/" + repo.name


def _prepare(goal: str, *, repo: Path, size: str, source_key: str, plan_spec: dict[str, Any],
             state_dir: Path | None) -> str:
    if not isinstance(goal, str) or not goal.strip():
        raise HiveError("goal is required; re-send a nonempty goal")
    task = ensure_task(
        goal.strip(), repo=_repo_label(repo), source_key=source_key,
        size=size, kind="development", state_dir=state_dir,
    )
    task_id = task["id"]
    mark_node(task_id, "intake", "succeeded", owner=SWARM_OWNER,
              artifact="swarm-intake", state_dir=state_dir)
    mark_node(task_id, "plan", "succeeded", owner=SWARM_OWNER,
              artifact="swarm-queen-plan", state_dir=state_dir)
    workplan_runtime.install(task_id, plan_spec, owner=SWARM_OWNER, state_dir=state_dir)
    return task_id


def swarm(goal: str, *, repo: Path, adapter: str | None = None,
          command_file: Path | None = None, plan: dict[str, Any] | None = None,
          size: str = "M", source_key: str = "", owner: str = SWARM_OWNER,
          max_seconds: int = 600, max_packages: int = 16, capacity: int = 4,
          write_paths: list[str] | None = None,
          state_dir: Path | None = None) -> dict[str, Any]:
    """Queen plans workers and soldiers, then drives. Never sets released."""
    adapter_name = resolve_adapter(adapter, command_file)
    repo = Path(repo)
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise HiveError("repo must be a git working copy")
    spec = plan if plan is not None else auto_plan(goal, repo=repo, write_paths=write_paths)
    key = source_key.strip() or ("swarm:" + goal.strip())
    task_id = _prepare(goal, repo=repo, size=size, source_key=key, plan_spec=spec, state_dir=state_dir)
    driven = executor.drive(
        task_id, repo=repo, owner=owner, adapter=adapter_name,
        command_file=command_file, max_seconds=max_seconds,
        max_packages=max_packages, capacity=capacity, state_dir=state_dir,
    )
    board = roster(task_id, state_dir=state_dir)
    terminal = driven.get("status") or "complete"
    if any(row.get("status") == "failed" for row in driven.get("dispatches") or []):
        terminal = "blocked"
    return {
        "task_id": task_id,
        "adapter": adapter_name,
        "terminal": terminal,
        "released": False,
        "roles": {
            "queen": 1,
            "worker": sum(1 for p in board["packages"] if p.get("role") == "worker"),
            "soldier": sum(1 for p in board["packages"] if p.get("role") == "soldier"),
        },
        "drive": driven,
        "roster": board,
    }


def load_plan_file(path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HiveError("cannot read workplan JSON: " + str(exc)) from exc
    if not isinstance(spec, dict):
        raise HiveError("workplan JSON must be an object")
    return spec
