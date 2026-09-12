"""Durable, fenced outbox records for Hive package execution."""
from __future__ import annotations

from copy import deepcopy
import secrets
import re
from pathlib import Path
from typing import Any

from .state import HiveError, _now, _read, _save, locked_task, state_directory
from . import scent
from . import workplan_runtime as runtime

_LIVE = {"pending", "starting", "running", "uncertain"}
_FINAL = {"succeeded", "failed"}


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"D-[0-9A-F]{32}", value):
        raise HiveError("invalid dispatch id")
    return value


def _entry(task: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
    dispatch_id = _id(dispatch_id)
    entries = task.get("dispatches")
    if not isinstance(entries, dict) or dispatch_id not in entries or not isinstance(entries[dispatch_id], dict):
        raise HiveError("dispatch not found")
    return entries[dispatch_id]


def _same(entry: dict[str, Any], owner: str, token: str) -> None:
    owner = runtime._owner(owner)
    if (entry.get("owner") != owner or not isinstance(token, str) or not token.isascii()
            or not isinstance(entry.get("token"), str) or not entry["token"].isascii()
            or not secrets.compare_digest(entry["token"], token)):
        raise HiveError("dispatch owner or token does not match")


def _live_package(task: dict[str, Any], entry: dict[str, Any], *, allow_expired=False) -> dict[str, Any]:
    if not runtime._task_accepts_work(task) or not runtime._plan_node_done(task):
        raise HiveError("task is not accepting dispatch transitions")
    plan = task.get("workplan")
    if plan is None:
        raise HiveError("workplan is not installed")
    package = runtime._package(plan, entry["package_id"])
    lease = package.get("lease")
    if (package.get("status") != "running" or not isinstance(lease, dict)
            or package.get("attempts") != entry.get("attempt")
            or lease.get("owner") != entry.get("owner")
            or not secrets.compare_digest(lease.get("token", ""), entry.get("token", ""))):
        raise HiveError("dispatch no longer owns the package attempt")
    if not allow_expired and runtime._expired(lease):
        raise HiveError("workplan lease has expired; reconcile before retrying")
    return package


def validate_dispatches(task: dict[str, Any]) -> None:
    if "dispatches" not in task:
        return
    entries = task["dispatches"]
    if not isinstance(entries, dict):
        raise HiveError("dispatches must be an object")
    for key, entry in entries.items():
        if _id(key) != key or not isinstance(entry, dict) or entry.get("id") != key:
            raise HiveError("invalid dispatch entry")
        required = {"id", "package_id", "attempt", "owner", "token", "status", "created_at"}
        if not required.issubset(entry) or entry.get("status") not in _LIVE | _FINAL:
            raise HiveError("invalid dispatch entry")
        if (not isinstance(entry["package_id"], str) or isinstance(entry["attempt"], bool)
                or not isinstance(entry["attempt"], int) or entry["attempt"] < 1):
            raise HiveError("invalid dispatch identity")
        if (not isinstance(entry["owner"], str) or not entry["owner"].strip()
                or not isinstance(entry["token"], str) or not entry["token"] or not entry["token"].isascii()):
            raise HiveError("invalid dispatch credentials")
        if not isinstance(entry["created_at"], str):
            raise HiveError("invalid dispatch timestamp")
        if "requested_adapter" in entry:
            adapter = entry["requested_adapter"]
            if not isinstance(adapter, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", adapter):
                raise HiveError("invalid requested execution adapter")
        if entry["status"] in {"starting", "running"} and not isinstance(entry.get("launch"), dict):
            raise HiveError("started dispatch requires launch")
        if entry["status"] == "running" and not isinstance(entry.get("session_id"), str):
            raise HiveError("running dispatch requires session")
        if entry["status"] == "succeeded" and (not isinstance(entry.get("session_id"), str) or not isinstance(entry.get("artifact"), dict)):
            raise HiveError("successful dispatch requires session and artifact")
    plan = task.get("workplan")
    if plan is None:
        if entries: raise HiveError("dispatches require workplan")
        return
    packages = {row["id"]: row for row in plan["packages"]}
    live = set()
    for entry in entries.values():
        if entry["package_id"] not in packages: raise HiveError("dispatch package is not in workplan")
        if entry["status"] in _LIVE:
            package = packages[entry["package_id"]]; lease = package.get("lease")
            identity = (entry["package_id"], entry["attempt"])
            if identity in live: raise HiveError("duplicate live dispatch attempt")
            live.add(identity)
            if (package.get("status") != "running" or package.get("attempts") != entry["attempt"]
                    or not isinstance(lease, dict) or lease.get("owner") != entry["owner"]
                    or lease.get("token") != entry["token"]):
                raise HiveError("live dispatch does not match package lease")


def get(task_id: str, dispatch_id: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    task = _read(task_id, state_directory(state_dir)); validate_dispatches(task)
    return deepcopy(_entry(task, dispatch_id))


def list_entries(task_id: str, *, state_dir: Path | None = None) -> list[dict[str, Any]]:
    task = _read(task_id, state_directory(state_dir)); validate_dispatches(task)
    return [deepcopy(task["dispatches"][key]) for key in sorted(task.get("dispatches", {}))]


def enqueue(task_id: str, package_id: str, *, owner: str, ttl_seconds: int = 900, adapter: str | None = None, state_dir: Path | None = None) -> dict[str, Any]:
    owner, ttl = runtime._owner(owner), runtime._ttl(ttl_seconds)
    if adapter is not None and (not isinstance(adapter, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", adapter)):
        raise HiveError("invalid requested execution adapter")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory); validate_dispatches(task)
        package = runtime._claim(task, package_id, owner, ttl)
        dispatch_id = "D-" + secrets.token_hex(16).upper()
        entry = {"id": dispatch_id, "package_id": package_id, "attempt": package["attempts"],
                 "owner": owner, "token": package["lease"]["token"], "status": "pending", "created_at": _now()}
        if adapter is not None:
            entry["requested_adapter"] = adapter
        task.setdefault("dispatches", {})[dispatch_id] = entry
        runtime._record(task, "dispatch_enqueued", dispatch_id=dispatch_id, package_id=package_id, owner=owner, attempt=package["attempts"])
        _save(task, directory); return deepcopy(entry)


def reserve(task_id: str, dispatch_id: str, *, owner: str, token: str, launch: dict, state_dir: Path | None = None) -> dict[str, Any]:
    if not isinstance(launch, dict) or not launch or any(not isinstance(k, str) or not k for k in launch):
        raise HiveError("launch must be a nonempty object")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory); entry = _entry(task, dispatch_id); _same(entry, owner, token)
        if entry["status"] != "pending": raise HiveError("dispatch is already reserved")
        _live_package(task, entry)
        entry.update(status="starting", launch=deepcopy(launch)); runtime._record(task, "dispatch_reserved", dispatch_id=dispatch_id)
        _save(task, directory); return deepcopy(entry)


def acknowledge(task_id: str, dispatch_id: str, *, owner: str, token: str, session_id: str,
                terminal_replay: bool = False, state_dir: Path | None = None) -> dict[str, Any]:
    if not isinstance(session_id, str) or not session_id.strip(): raise HiveError("session_id is required")
    if type(terminal_replay) is not bool: raise HiveError("terminal_replay must be boolean")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory); entry = _entry(task, dispatch_id); _same(entry, owner, token)
        if entry.get("session_id") is not None and entry.get("session_id") != session_id:
            raise HiveError("dispatch session_id cannot be replaced")
        if terminal_replay and entry["status"] in _FINAL and entry.get("session_id") == session_id:
            return deepcopy(entry)
        if entry["status"] == "running" and entry.get("session_id") == session_id: return deepcopy(entry)
        allowed = {"starting"} if not terminal_replay else {"starting", "running", "uncertain"}
        if entry["status"] not in allowed: raise HiveError("dispatch cannot be acknowledged")
        _live_package(task, entry, allow_expired=terminal_replay)
        entry.update(status="running", session_id=session_id); runtime._record(task, "dispatch_acknowledged", dispatch_id=dispatch_id, session_id=session_id)
        _save(task, directory); return deepcopy(entry)


def settle(task_id: str, dispatch_id: str, *, owner: str, token: str, success: bool, artifact: str, reason: str = "", state_dir: Path | None = None) -> dict[str, Any]:
    if type(success) is not bool: raise HiveError("success must be boolean")
    receipt = runtime._artifact(artifact)
    if not success: reason = runtime._reason(reason)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory); entry = _entry(task, dispatch_id); _same(entry, owner, token)
        desired = "succeeded" if success else "failed"
        if entry["status"] in _FINAL:
            if entry["status"] == desired and entry.get("artifact") == receipt and entry.get("reason", "") == reason: return deepcopy(entry)
            raise HiveError("terminal dispatch receipt differs")
        package = _live_package(task, entry, allow_expired=True)
        if success and not entry.get("session_id"): raise HiveError("successful dispatch requires session acknowledgement")
        package.update(status=desired, lease=None)
        if success:
            package["artifact"] = receipt
            scent.on_finish(task, package, by=entry["owner"])
        else:
            package["failure_reason"] = reason
            scent.on_fail(task, package, by=entry["owner"])
        entry.update(status=desired, artifact=receipt, reason=reason, settled_at=_now())
        runtime._record(task, "dispatch_settled", dispatch_id=dispatch_id, success=success, artifact=receipt, reason=reason)
        _save(task, directory); return deepcopy(entry)


def mark_uncertain(task_id: str, dispatch_id: str, *, owner: str, token: str, reason: str, state_dir: Path | None = None) -> dict[str, Any]:
    reason = runtime._reason(reason)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory); entry = _entry(task, dispatch_id); _same(entry, owner, token)
        if entry["status"] == "uncertain" and entry.get("reason") == reason: return deepcopy(entry)
        if entry["status"] not in {"pending", "starting", "running"}: raise HiveError("dispatch cannot become uncertain")
        _live_package(task, entry, allow_expired=True)
        entry.update(status="uncertain", reason=reason, uncertain_at=_now()); runtime._record(task, "dispatch_uncertain", dispatch_id=dispatch_id, reason=reason)
        _save(task, directory); return deepcopy(entry)
