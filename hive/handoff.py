"""Durable handoff protocol for an editor or other external executor.

This adapter never starts or observes a remote process.  Its only authority is
the dispatch capability issued to the package owner, and all external claims
are recorded as attested evidence rather than scheduler observations.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import hashlib
import json
import os
import re

from . import dispatch, workspaces
from .state import HiveError, _now, load_task, state_directory
from .workplan_runtime import heartbeat as package_heartbeat


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,99}\Z")


def _helpers():
    # Kept local: executor imports this adapter when routing external entries.
    from .executor import _directory, _write, _read, _digest, _package, _prompt
    return _directory, _write, _read, _digest, _package, _prompt


def _dir(task_id, dispatch_id, directory):
    return _helpers()[0](task_id, dispatch_id, directory)


@contextmanager
def _locked(task_id, dispatch_id, directory):
    """Serialize local evidence writes; dispatch owns its separate state lock."""
    execution_dir = _dir(task_id, dispatch_id, directory)
    execution_dir.mkdir(parents=True, exist_ok=True)
    with (execution_dir / "external.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield execution_dir


def _label(value):
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise HiveError("external executor name must be a safe nonempty label")
    return value


def _report(value, entry):
    required = {"task_id", "dispatch_id", "attempt", "session_id", "status", "summary", "remaining"}
    if not isinstance(value, dict) or set(value) != required:
        raise HiveError("external report schema is invalid")
    if (value["task_id"] != entry.get("task_id", None) or value["dispatch_id"] != entry["id"]
            or type(value["attempt"]) is not int or value["attempt"] != entry["attempt"]):
        # task_id is supplied by caller below, so preserve a single generic failure.
        raise HiveError("external report identity does not match dispatch")
    if (not isinstance(value["session_id"], str) or not value["session_id"].strip()
            or value["status"] not in {"completed", "blocked"}
            or not isinstance(value["summary"], str)
            or not isinstance(value["remaining"], list)
            or any(not isinstance(item, str) for item in value["remaining"])):
        raise HiveError("external report fields are invalid")
    return value


def _reason(raw, scope):
    if scope.get("violations"): return "workspace scope violation"
    text = raw["summary"].strip()
    if text: return text
    if raw["remaining"]: return "external work remains"
    return "external executor reported blocked"


def _checkpoint(saved, entry, package):
    if saved.get("workspace") != entry["launch"].get("workspace") or saved.get("write_paths") != package["write_paths"]:
        raise HiveError("external checkpoint identity does not match dispatch")
    inspection = saved.get("inspection")
    if not isinstance(inspection, dict) or inspection.get("base_sha") != entry["launch"]["workspace"].get("base_sha"):
        raise HiveError("external checkpoint inspection is invalid")
    return inspection


def prepare(task_id, package_id, *, repo, owner, executor_name, base_ref="HEAD",
            resume_checkpoint=None, state_dir=None):
    """Claim a package and write a portable brief; no external program is run."""
    directory = state_directory(state_dir)
    executor_name = _label(executor_name)
    task = load_task(task_id, state_dir=directory)
    from .delivery import _context_repo
    repo = Path(repo)
    _context_repo(task, repo)
    workspace_id = hashlib.sha256((task_id + ":" + package_id).encode()).hexdigest()
    workspace = workspaces.prepare(repo, base_ref, workspace_id=workspace_id, root=directory / "workspaces")
    package = next((row for row in task.get("workplan", {}).get("packages", []) if row.get("id") == package_id), None)
    if not isinstance(package, dict): raise HiveError("workplan package not found")
    observed = workspaces.inspect(workspace, package["write_paths"])
    if resume_checkpoint is not None:
        workspaces.assert_checkpoint(workspace, Path(resume_checkpoint))
    elif not observed["clean"]:
        # The pending entry is deliberately retained, and the tree is untouched.
        raise HiveError("existing work requires an explicit verified resume checkpoint")
    if observed["violations"]:
        raise HiveError("workspace has out-of-scope changes")
    entry = dispatch.enqueue(task_id, package_id, owner=owner, ttl_seconds=900, adapter="external", state_dir=directory)
    # The claim can have changed the plan; bind the brief to its actual attempt.
    task = load_task(task_id, state_dir=directory); package = _helpers()[4](task, entry)
    execution_dir = _dir(task_id, entry["id"], directory)
    execution_dir.mkdir(parents=True, exist_ok=True)
    brief = {"protocol_version": 1, "adapter": "external", "executor_name": executor_name,
             "task_id": task_id, "dispatch_id": entry["id"], "attempt": entry["attempt"],
             "workspace": workspace, "worktree": workspace["worktree"], "repo": str(repo.resolve()),
             "base_sha": workspace["base_sha"], "execution_dir": str(execution_dir),
             "acceptance": package["acceptance"], "write_paths": package["write_paths"],
             "read_paths": package["read_paths"], "shared_repo_rules": _helpers()[5](task, package),
             "report_schema": {"task_id": task_id, "dispatch_id": entry["id"], "attempt": entry["attempt"],
                               "session_id": "external immutable session id", "status": "completed|blocked",
                               "summary": "string", "remaining": ["strings"]}}
    _helpers()[1](execution_dir / "brief.json", brief)
    launch = {"adapter": "external", "executor_name": executor_name, "protocol_version": 1,
              "repo": str(repo.resolve()), "base_sha": workspace["base_sha"], "worktree": workspace["worktree"],
              "workspace": workspace, "execution_dir": str(execution_dir), "reserved_at": _now(),
              "brief": str(execution_dir / "brief.json")}
    dispatch.reserve(task_id, entry["id"], owner=entry["owner"], token=entry["token"], launch=launch, state_dir=directory)
    return {"dispatch_id": entry["id"], "owner": entry["owner"], "token": entry["token"],
            "worktree": workspace["worktree"], "brief": str(execution_dir / "brief.json"), "status": "starting"}


def attach(task_id, dispatch_id, *, owner, token, session_id, state_dir=None):
    directory = state_directory(state_dir); entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if entry.get("launch", {}).get("adapter") != "external": raise HiveError("dispatch is not an external handoff")
    if not isinstance(session_id, str) or not session_id.strip(): raise HiveError("session_id is required")
    with _locked(task_id, dispatch_id, directory) as execution_dir:
        attached = dispatch.acknowledge(task_id, dispatch_id, owner=owner, token=token, session_id=session_id, state_dir=directory)
        session_path = execution_dir / "session.json"
        if session_path.exists():
            if _helpers()[2](session_path).get("session_id") != session_id: raise HiveError("external session evidence cannot be replaced")
        else: _helpers()[1](session_path, {"session_id": session_id, "provenance": "external-attested", "attached_at": _now()})
        return attached


def heartbeat(task_id, dispatch_id, *, owner, token, state_dir=None):
    directory = state_directory(state_dir); entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if entry.get("launch", {}).get("adapter") != "external": raise HiveError("dispatch is not an external handoff")
    # authenticate through dispatch before refreshing the package lease.
    if entry["owner"] != owner or entry["token"] != token: raise HiveError("dispatch owner or token does not match")
    with _locked(task_id, dispatch_id, directory) as execution_dir:
        if (execution_dir / "cancel.json").exists(): raise HiveError("cancellation was requested")
        package_heartbeat(task_id, entry["package_id"], owner=owner, token=token, state_dir=directory)
        return dispatch.get(task_id, dispatch_id, state_dir=directory)


def submit(task_id, dispatch_id, *, owner, token, report: Path, state_dir=None):
    directory = state_directory(state_dir); entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if entry.get("launch", {}).get("adapter") != "external": raise HiveError("dispatch is not an external handoff")
    if entry["owner"] != owner or entry["token"] != token: raise HiveError("dispatch owner or token does not match")
    with _locked(task_id, dispatch_id, directory) as execution_dir:
        raw = _helpers()[2](Path(report)); check = dict(entry); check["task_id"] = task_id; _report(raw, check)
        session = _helpers()[2](execution_dir / "session.json")
        if raw["session_id"] != session.get("session_id") or entry.get("session_id") != raw["session_id"]: raise HiveError("external report session does not match attached session")
        immutable = execution_dir / "external-report.json"
        if immutable.exists():
            if _helpers()[2](immutable) != raw: raise HiveError("terminal external report differs")
            return _reconcile(task_id, dispatch_id, directory, execution_dir)
        cancelled = (execution_dir / "cancel.json").exists()
        if cancelled and raw["status"] == "completed": raise HiveError("cancellation was requested; external report cannot settle success")
    # A new terminal receipt is accepted only while this capability still owns
    # the live package lease; a replay above never mutates a settled attempt.
        package_heartbeat(task_id, entry["package_id"], owner=owner, token=token, state_dir=directory)
        package = _helpers()[4](load_task(task_id, state_dir=directory), entry)
        observed = workspaces.inspect(entry["launch"]["workspace"], package["write_paths"])
        success = raw["status"] == "completed" and not raw["remaining"] and not observed["violations"] and not cancelled
        remaining = raw["remaining"] if not observed["violations"] else raw["remaining"] + observed["violations"]
        checkpoint = execution_dir / "checkpoint.json"
        workspaces.checkpoint(entry["launch"]["workspace"], package["write_paths"], remaining=remaining, output=checkpoint)
        _helpers()[1](immutable, raw)
        result = {"task_id": task_id, "dispatch_id": dispatch_id, "attempt": entry["attempt"], "session_id": raw["session_id"], "success": success,
                  "reason": "" if success else _reason(raw, observed), "summary": raw,
                  "provenance": "external-attested", "checkpoint": str(checkpoint), "checkpoint_sha256": _helpers()[3](checkpoint), "scope": observed,
                  "external_report_sha256": _helpers()[3](immutable), "at": _now()}
        result_path = execution_dir / "result.json"
        if result_path.exists() and _helpers()[2](result_path) != result: raise HiveError("terminal external result differs")
        if not result_path.exists(): _helpers()[1](result_path, result)
        return _reconcile(task_id, dispatch_id, directory, execution_dir)


def _reconcile(task_id, dispatch_id, directory, execution_dir):
    """Reconcile under ``_locked``; never infer a remote process state."""
    entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    result_path = execution_dir / "result.json"
    if not result_path.exists():
        report_path, checkpoint = execution_dir / "external-report.json", execution_dir / "checkpoint.json"
        if not report_path.exists(): return entry
        raw = _helpers()[2](report_path); check = dict(entry); check["task_id"] = task_id; _report(raw, check)
        session = _helpers()[2](execution_dir / "session.json")
        if raw["session_id"] != session.get("session_id") or raw["session_id"] != entry.get("session_id"): raise HiveError("external session evidence mismatch")
        saved = _helpers()[2](checkpoint); package = _helpers()[4](load_task(task_id, state_dir=directory), entry); scope = _checkpoint(saved, entry, package)
        remaining = raw["remaining"] if not scope.get("violations") else raw["remaining"] + scope["violations"]
        if saved.get("remaining") != remaining: raise HiveError("external report differs from checkpoint")
        success = raw["status"] == "completed" and not raw["remaining"] and not scope.get("violations") and not (execution_dir / "cancel.json").exists()
        _helpers()[1](result_path, {"task_id": task_id, "dispatch_id": dispatch_id, "attempt": entry["attempt"], "session_id": raw["session_id"], "success": success,
            "reason": "" if success else _reason(raw, scope), "summary": raw, "provenance": "external-attested", "checkpoint": str(checkpoint), "checkpoint_sha256": _helpers()[3](checkpoint), "scope": scope, "external_report_sha256": _helpers()[3](report_path), "at": _now()})
    result = _helpers()[2](result_path)
    if (result.get("task_id") != task_id or result.get("dispatch_id") != dispatch_id or result.get("attempt") != entry["attempt"]
            or type(result.get("success")) is not bool or result.get("provenance") != "external-attested"):
        raise HiveError("external result identity does not match dispatch")
    checkpoint = execution_dir / "checkpoint.json"; report_path = execution_dir / "external-report.json"
    if result.get("checkpoint") != str(checkpoint) or result.get("checkpoint_sha256") != _helpers()[3](checkpoint): raise HiveError("external checkpoint digest mismatch")
    if result.get("external_report_sha256") != _helpers()[3](report_path): raise HiveError("external report digest mismatch")
    raw = _helpers()[2](report_path); check = dict(entry); check["task_id"] = task_id; _report(raw, check)
    session = _helpers()[2](execution_dir / "session.json")
    if raw["session_id"] != session.get("session_id") or raw["session_id"] != entry.get("session_id") or result.get("session_id") != raw["session_id"]: raise HiveError("external session evidence mismatch")
    saved = _helpers()[2](checkpoint); package = _helpers()[4](load_task(task_id, state_dir=directory), entry); _checkpoint(saved, entry, package)
    if saved.get("inspection") != result.get("scope") or saved.get("remaining") != (raw["remaining"] if not result["scope"].get("violations") else raw["remaining"] + result["scope"]["violations"]): raise HiveError("external result does not match checkpoint")
    expected_success = raw["status"] == "completed" and not raw["remaining"] and not result["scope"].get("violations") and not (execution_dir / "cancel.json").exists()
    if result["success"] != expected_success: raise HiveError("external result success does not match report")
    if result["success"] and entry["status"] not in {"succeeded", "failed"}:
        workspaces.assert_checkpoint(entry["launch"]["workspace"], checkpoint)
        dispatch.acknowledge(task_id, dispatch_id, owner=entry["owner"], token=entry["token"], session_id=result["session_id"], terminal_replay=True, state_dir=directory)
    return dispatch.settle(task_id, dispatch_id, owner=entry["owner"], token=entry["token"], success=result["success"], artifact=str(result_path), reason=result.get("reason", ""), state_dir=directory)


def reconcile(task_id, dispatch_id, *, state_dir=None):
    directory = state_directory(state_dir); entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if entry.get("launch", {}).get("adapter") != "external": raise HiveError("dispatch is not an external handoff")
    with _locked(task_id, dispatch_id, directory) as execution_dir:
        return _reconcile(task_id, dispatch_id, directory, execution_dir)


def cancel(task_id, dispatch_id, *, owner, reason, state_dir=None):
    directory = state_directory(state_dir); entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if owner != entry["owner"] or not isinstance(reason, str) or not reason.strip(): raise HiveError("cancellation requires the dispatch owner and a reason")
    with _locked(task_id, dispatch_id, directory) as execution_dir:
        # A submit may have completed while this caller waited for the lock.
        current = dispatch.get(task_id, dispatch_id, state_dir=directory)
        if current["status"] in {"succeeded", "failed"}: return current
        if (execution_dir / "result.json").exists() or (execution_dir / "external-report.json").exists():
            settled = _reconcile(task_id, dispatch_id, directory, execution_dir)
            if settled["status"] in {"succeeded", "failed"}: return settled
        path = execution_dir / "cancel.json"
        if path.exists(): return {"dispatch_id": dispatch_id, "status": "cancellation_requested"}
        _helpers()[1](path, {"at": _now(), "owner": owner, "reason": reason, "remote_process": "not observed or stopped"})
        return {"dispatch_id": dispatch_id, "status": "cancellation_requested"}
