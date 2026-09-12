"""Coordinator entry: one goal, one bounded drive, a visible roster.

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

from . import dispatch, executor, workplan_runtime
from .intake import ensure_task
from .state import HiveError, load_task, mark_node, state_directory


SWARM_OWNER = "swarm"
DEFAULT_WRITE = ".hive-swarm-output"


def resolve_adapter(adapter: str | None, command_file: str | Path | None) -> str:
    name = (adapter or os.environ.get("HIVE_EXECUTOR_ADAPTER") or "").strip()
    if not name:
        raise HiveError("adapter is required; pass --adapter or set HIVE_EXECUTOR_ADAPTER")
    if name == "command" and not command_file:
        raise HiveError("command adapter requires --command-file")
    return name


def default_plan(goal: str, *, write_paths: list[str] | None = None) -> dict[str, Any]:
    paths = list(write_paths or [DEFAULT_WRITE])
    return {
        "max_parallel": 2,
        "packages": [{
            "id": "worker-1",
            "title": goal.strip()[:200] or "swarm",
            "depends_on": [],
            "read_paths": [],
            "write_paths": paths,
            "acceptance": ["bounded swarm package; not a release"],
            "priority": 50,
            "max_attempts": 2,
        }],
    }


def roster(task_id: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    directory = state_directory(state_dir)
    task = load_task(task_id, state_dir=directory)
    packages = []
    plan = task.get("workplan")
    if isinstance(plan, dict):
        for package in plan.get("packages") or []:
            lease = package.get("lease") or {}
            packages.append({
                "package_id": package.get("id"),
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
        "packages": packages,
        "dispatches": [
            {"dispatch_id": row.get("id"), "status": row.get("status"),
             "package_id": row.get("package_id")}
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
              artifact="swarm-default-plan", state_dir=state_dir)
    workplan_runtime.install(task_id, plan_spec, owner=SWARM_OWNER, state_dir=state_dir)
    return task_id


def swarm(goal: str, *, repo: Path, adapter: str | None = None,
          command_file: Path | None = None, plan: dict[str, Any] | None = None,
          size: str = "M", source_key: str = "", owner: str = SWARM_OWNER,
          max_seconds: int = 600, max_packages: int = 3, capacity: int = 2,
          write_paths: list[str] | None = None,
          state_dir: Path | None = None) -> dict[str, Any]:
    """Intake, install one plan, drive. Never sets released."""
    adapter_name = resolve_adapter(adapter, command_file)
    repo = Path(repo)
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise HiveError("repo must be a git working copy")
    spec = plan if plan is not None else default_plan(goal, write_paths=write_paths)
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
