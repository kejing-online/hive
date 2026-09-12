"""Persistent, fenced execution leases for a Hive task workplan.

This module deliberately stores its state in the existing Hive task JSON.  It
does not advance the fixed Hive DAG or treat a package artifact as verification
or release evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import secrets
from typing import Any

from .state import HiveError, _now, _read, _save, locked_task, state_directory
from . import scent
from .workplan import normalize_plan, select_ready, validate_plan


_STOPPED = {"failed", "interrupted"}
_STATIC_PACKAGE_FIELDS = ("id", "title", "depends_on", "read_paths", "write_paths", "acceptance", "priority", "max_attempts")


def _owner(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HiveError("workplan owner is required")
    return value.strip()


def _reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HiveError("reason is required")
    return value.strip()


def _ttl(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
        raise HiveError("ttl_seconds must be an integer from 1 through 86400")
    return value


def _package(plan: dict[str, Any], package_id: str) -> dict[str, Any]:
    if not isinstance(package_id, str) or not package_id:
        raise HiveError("package id is required")
    package = next((row for row in plan["packages"] if row["id"] == package_id), None)
    if package is None:
        raise HiveError(f"workplan package not found: {package_id}")
    return package


def _expires_at(ttl: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()


def _expired(lease: dict[str, Any]) -> bool:
    try:
        expires = datetime.fromisoformat(lease["expires_at"])
        if expires.tzinfo is None:
            return True
    except (KeyError, TypeError, ValueError):
        return True
    return expires <= datetime.now(timezone.utc)


def _task_accepts_work(task: dict[str, Any]) -> bool:
    # State only emits ``active`` for a runnable development task.  Be strict
    # about that value so an unfamiliar future state cannot accidentally issue
    # a lease.  A stopped whole-task executor and explicit freeze markers also
    # fence package work even if an old state file still says active.
    execution = task.get("execution") or {}
    return (task.get("kind", "development") == "development"
            and task.get("status") == "active"
            and execution.get("status") not in _STOPPED
            and not task.get("frozen")
            and not task.get("freeze"))


def _task_block_reason(task: dict[str, Any]) -> str:
    if task.get("kind", "development") != "development":
        return "task is not a development task"
    if task.get("status") != "active":
        return f"task status is {task.get('status', 'unknown')}"
    if (task.get("execution") or {}).get("status") in _STOPPED:
        return "task workflow is stopped"
    if task.get("frozen") or task.get("freeze"):
        return "task is frozen"
    if not _plan_node_done(task):
        return "planning is not succeeded"
    return "task is not accepting workplan work"


def _plan_node_done(task: dict[str, Any]) -> bool:
    return any(node.get("id") == "plan" and node.get("status") == "succeeded" for node in task.get("nodes", []))


def _already_executed(task: dict[str, Any]) -> bool:
    return (any(node.get("id") == "implement" and node.get("status") == "succeeded" for node in task.get("nodes", []))
            or bool(task.get("delivery"))
            or any(row.get("event") == "delivery_registered" for row in task.get("history", [])))


def _record(task: dict[str, Any], event: str, **fields: Any) -> None:
    task.setdefault("history", []).append({"event": event, "at": _now(), **deepcopy(fields)})


def _static_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The immutable installation contract, excluding package execution state."""
    return {
        "schema_version": plan["schema_version"],
        "max_parallel": plan["max_parallel"],
        "packages": [{field: deepcopy(package[field]) for field in _STATIC_PACKAGE_FIELDS}
                     for package in plan["packages"]],
    }


def install(task_id: str, spec: dict[str, Any], *, owner: str, state_dir: Path | None = None) -> dict[str, Any]:
    """Install a normalized plan once planning is complete; identical replay is safe."""
    owner = _owner(owner)
    plan = normalize_plan(spec)
    validate_plan(plan)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        current = task.get("workplan")
        if current is not None:
            validate_plan(current)
            if _static_plan(current) == _static_plan(plan):
                return deepcopy(current)
            raise HiveError("workplan already installed and cannot be replaced")
        if not _task_accepts_work(task) or not _plan_node_done(task):
            raise HiveError("workplan installation requires an active development task with planning complete")
        if _already_executed(task):
            raise HiveError("workplan cannot be installed after implementation or delivery registration")
        task["workplan"] = plan
        scent.on_queen_plan(task, plan["packages"], by=owner)
        _record(task, "workplan_installed", owner=owner)
        _save(task, directory)
        return deepcopy(plan)


def status(task_id: str, *, capacity: int | None = None, state_dir: Path | None = None) -> dict[str, Any]:
    """Read a workplan projection.  Expired leases remain untouched until recover."""
    task = _read(task_id, state_directory(state_dir))
    plan = task.get("workplan")
    if plan is None:
        raise HiveError("workplan is not installed")
    validate_plan(plan)
    projection = select_ready(deepcopy(plan), capacity=capacity, scent_field=scent.field(task))
    if not _task_accepts_work(task) or not _plan_node_done(task):
        reason = _task_block_reason(task)
        projection["blocked"] = {**projection["blocked"], **{
            package["id"]: [reason] for package in plan["packages"] if package["status"] == "queued"
        }}
        projection["ready"] = []
    return {"task_id": task_id, "revision": task["revision"], "plan": deepcopy(plan), **projection}


def _claim(task: dict[str, Any], package_id: str, owner: str, ttl: int) -> dict[str, Any]:
    """Claim a ready package in an already locked task, without saving it."""
    if not _task_accepts_work(task) or not _plan_node_done(task):
        raise HiveError("task is not accepting workplan claims")
    plan = task.get("workplan")
    if plan is None:
        raise HiveError("workplan is not installed")
    validate_plan(plan)
    package = _package(plan, package_id)
    if package_id not in select_ready(plan, scent_field=scent.field(task)).get("ready", []):
        raise HiveError("workplan package is not ready")
    if package["attempts"] >= package["max_attempts"]:
        raise HiveError("workplan package has exhausted its attempts")
    package["attempts"] += 1
    package["status"] = "running"
    package["lease"] = {"token": secrets.token_urlsafe(24), "owner": owner, "expires_at": _expires_at(ttl)}
    scent.on_claim(task, package, by=owner)
    _record(task, "workplan_claimed", package_id=package_id, owner=owner, attempt=package["attempts"])
    return package


def claim(task_id: str, package_id: str, *, owner: str, ttl_seconds: int = 900,
          state_dir: Path | None = None) -> dict[str, Any]:
    owner, ttl = _owner(owner), _ttl(ttl_seconds)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        package = _claim(task, package_id, owner, ttl)
        _save(task, directory)
        return deepcopy(package)


def _leased_package(task: dict[str, Any], package_id: str, owner: str, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = task.get("workplan")
    if plan is None:
        raise HiveError("workplan is not installed")
    validate_plan(plan)
    package = _package(plan, package_id)
    lease = package.get("lease")
    if package.get("status") != "running" or not isinstance(lease, dict):
        raise HiveError("workplan package has no active lease")
    lease_token = lease.get("token")
    if (not isinstance(token, str) or not token.isascii() or not isinstance(lease_token, str)
            or not lease_token.isascii() or not secrets.compare_digest(lease_token, token)):
        raise HiveError("workplan lease token does not match")
    if lease.get("owner") != owner:
        raise HiveError("workplan lease owner does not match")
    if _expired(lease):
        raise HiveError("workplan lease has expired; recover it before retrying")
    return package, lease


def heartbeat(task_id: str, package_id: str, *, owner: str, token: str, ttl_seconds: int = 900,
              state_dir: Path | None = None) -> dict[str, Any]:
    owner, ttl = _owner(owner), _ttl(ttl_seconds)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        if not _task_accepts_work(task) or not _plan_node_done(task):
            raise HiveError("task is not accepting workplan heartbeats")
        package, lease = _leased_package(task, package_id, owner, token)
        lease["expires_at"] = _expires_at(ttl)
        # busy decays faster than the default lease; re-lay it on renewal so the
        # slice never looks free while a live holder still owns it.
        writes = list(package.get("write_paths") or [])
        scent.evaporate(task, paths=writes, kinds=("busy",))
        for path in writes:
            scent.deposit(task, path=path, kind="busy", by=owner, package_id=package_id)
        _record(task, "workplan_heartbeat", package_id=package_id, owner=owner)
        _save(task, directory)
        return deepcopy(package)


def _artifact(value: str) -> dict[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise HiveError("artifact path is required")
    try:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise HiveError("artifact must be an existing local file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except HiveError:
        raise
    except (OSError, RuntimeError) as exc:
        raise HiveError("artifact must be a readable local file") from exc
    return {"path": str(path), "sha256": digest.hexdigest()}


def _active_dispatch(task: dict[str, Any], package_id: str, owner: str, token: str) -> bool:
    """Fence legacy lease receipts while an outbox owns this exact attempt."""
    for entry in (task.get("dispatches") or {}).values():
        if (isinstance(entry, dict) and entry.get("package_id") == package_id
                and entry.get("owner") == owner and entry.get("token") == token
                and entry.get("status") in {"pending", "starting", "running", "uncertain"}):
            return True
    return False


def finish(task_id: str, package_id: str, *, owner: str, token: str, artifact: str,
           state_dir: Path | None = None) -> dict[str, Any]:
    owner, receipt = _owner(owner), _artifact(artifact)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        if not _task_accepts_work(task) or not _plan_node_done(task):
            raise HiveError("task is not accepting workplan completion")
        if _active_dispatch(task, package_id, owner, token):
            raise HiveError("active dispatch must be settled through the outbox")
        package, _lease = _leased_package(task, package_id, owner, token)
        package.update(status="succeeded", lease=None, artifact=receipt)
        scent.on_finish(task, package, by=owner)
        _record(task, "workplan_finished", package_id=package_id, owner=owner, artifact=receipt)
        _save(task, directory)
        return deepcopy(package)


def fail(task_id: str, package_id: str, *, owner: str, token: str, reason: str,
         state_dir: Path | None = None) -> dict[str, Any]:
    owner, reason = _owner(owner), _reason(reason)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        if not _task_accepts_work(task) or not _plan_node_done(task):
            raise HiveError("task is not accepting workplan failure receipts")
        if _active_dispatch(task, package_id, owner, token):
            raise HiveError("active dispatch must be settled through the outbox")
        package, _lease = _leased_package(task, package_id, owner, token)
        package.update(status="failed", lease=None, failure_reason=reason)
        scent.on_fail(task, package, by=owner)
        _record(task, "workplan_failed", package_id=package_id, owner=owner, reason=reason)
        _save(task, directory)
        return deepcopy(package)


def recover(task_id: str, *, owner: str, reason: str, state_dir: Path | None = None) -> dict[str, Any]:
    owner, reason = _owner(owner), _reason(reason)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        plan = task.get("workplan")
        if plan is None:
            raise HiveError("workplan is not installed")
        validate_plan(plan)
        if any(isinstance(entry, dict) and entry.get("status") in {"pending", "starting", "running", "uncertain"}
               for entry in (task.get("dispatches") or {}).values()):
            raise HiveError("active dispatch must be reconciled through the outbox")
        changed = []
        for package in plan["packages"]:
            if package.get("status") == "running" and isinstance(package.get("lease"), dict) and _expired(package["lease"]):
                previous = deepcopy(package["lease"])
                package.update(status="interrupted", lease=None, interruption_reason=reason)
                # The holder is gone with its lease; its busy trace must go too.
                scent.evaporate(task, paths=list(package.get("write_paths") or []), kinds=("busy",))
                changed.append({"package_id": package["id"], "lease": previous})
        if changed:
            _record(task, "workplan_recovered", owner=owner, reason=reason, packages=changed)
            _save(task, directory)
        return deepcopy(plan)


def retry(task_id: str, package_id: str, *, owner: str, reason: str,
          state_dir: Path | None = None) -> dict[str, Any]:
    owner, reason = _owner(owner), _reason(reason)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        if not _task_accepts_work(task) or not _plan_node_done(task):
            raise HiveError("task is not accepting workplan retries")
        plan = task.get("workplan")
        if plan is None:
            raise HiveError("workplan is not installed")
        validate_plan(plan)
        package = _package(plan, package_id)
        if package.get("status") not in _STOPPED:
            raise HiveError("only a stopped workplan package can be retried")
        if package["attempts"] >= package["max_attempts"]:
            raise HiveError("workplan package has exhausted its attempts")
        package.update(status="queued", lease=None)
        if str(package.get("id") or "").startswith("worker-"):
            for path in package.get("write_paths") or []:
                scent.deposit(task, path=path, kind="need", by=owner, package_id=str(package["id"]))
        _record(task, "workplan_retried", package_id=package_id, owner=owner, reason=reason)
        _save(task, directory)
        return deepcopy(package)
