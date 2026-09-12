"""Shared Hive task state. Locked updates fail closed; no model or production calls.

State lives outside runtime worktrees. Set HIVE_STATE_DIR or pass state_dir for isolated tests.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

DEFAULT_DIR = Path(os.environ.get("HIVE_STATE_DIR") or Path.home() / ".hive")
DONE = {"succeeded", "skipped"}
ACTIVE = {"leased", "running"}
STOPPED = {"failed", "blocked", "interrupted"}
STATUSES = {"queued", *ACTIVE, *STOPPED, *DONE}
RISK = {"read", "propose", "approved_execute", "release"}


class HiveError(ValueError):
    pass


def state_directory(value: Path | None = None) -> Path:
    directory = Path(value) if value is not None else Path(os.environ.get("HIVE_STATE_DIR") or DEFAULT_DIR)
    return directory.expanduser().absolute()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nodes(size: str, kind: str = "development") -> list[dict[str, Any]]:
    if kind not in {"development", "analysis"}:
        raise HiveError("kind must be development or analysis")
    shared = [
        ("intake", [], "read"), ("plan", ["intake"], "read"),
        ("implement", ["plan"], "propose"), ("verify", ["implement"], "read"),
        ("review", ["verify"], "read"), ("integrate", ["review"], "release"),
        ("release", ["integrate"], "release"),
    ]
    if size in {"M", "L"}:
        shared.insert(2, ("research", ["plan"], "read"))
        shared[3] = ("implement", ["plan", "research"], "propose")
    if size == "L":
        shared.insert(-2, ("security", ["verify"], "read"))
        shared[-2] = ("integrate", ["review", "security"], "release")
    if kind == "analysis":
        shared = [item for item in shared if item[0] in {"intake", "plan", "research"}]
        shared.append(("review", ["research" if size in {"M", "L"} else "plan"], "read"))
    return [{"id": name, "depends_on": deps, "risk": risk, "status": "queued",
             "owner": None, "artifacts": [], "updated_at": _now()} for name, deps, risk in shared]


def _path(task_id: str, state_dir: Path) -> Path:
    if not isinstance(task_id, str) or not re.fullmatch(r"HIVE-[A-Z0-9-]{1,100}", task_id):
        raise HiveError("invalid Hive task id")
    return state_dir / f"{task_id}.json"


@contextmanager
def locked_task(task_id: str, state_dir: Path | None = None):
    directory = state_directory(state_dir)
    target = _path(task_id, directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(".lock")
    if lock.is_symlink() or target.is_symlink():
        raise HiveError("Hive state must not be a symlink")
    try:
        import fcntl
    except ImportError as exc:
        raise HiveError("Hive file lock requires POSIX fcntl") from exc
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def validate_task(task: dict[str, Any]) -> None:
    """Validate the entire contract, including absent/duplicate/relinked gates."""
    if not isinstance(task, dict) or task.get("schema_version") != 2 or task.get("size") not in {"S", "M", "L"}:
        raise HiveError("invalid Hive task schema")
    _path(task.get("id"), Path("."))
    expected = _nodes(task["size"], task.get("kind", "development"))
    nodes = task.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(expected):
        raise HiveError("task node graph is incomplete")
    if not all(isinstance(n, dict) for n in nodes) or [n.get("id") for n in nodes] != [n["id"] for n in expected]:
        raise HiveError("task node graph has missing, duplicate or unexpected nodes")
    for node, spec in zip(nodes, expected):
        if node.get("depends_on") != spec["depends_on"] or node.get("risk") != spec["risk"]:
            raise HiveError("task dependencies or risk were changed")
        if node.get("status") not in STATUSES:
            raise HiveError("invalid node status")
        if not isinstance(node.get("artifacts"), list) or any(not isinstance(a, str) or not a.strip() for a in node["artifacts"]):
            raise HiveError("invalid evidence artifacts")
        if node.get("owner") is not None and (not isinstance(node["owner"], str) or not node["owner"].strip()):
            raise HiveError("invalid node owner")
        if node["status"] in ACTIVE | DONE and not node.get("owner"):
            raise HiveError("active or completed node requires an owner")
        if node["status"] == "skipped" and (node["id"] != "research" or not node.get("approved_at") or not node["artifacts"]):
            raise HiveError("only research may be explicitly waived with evidence")
    if not isinstance(task.get("history"), list) or not isinstance(task.get("revision"), int):
        raise HiveError("invalid task history/revision")
    if "workplan" in task:
        from .workplan import validate_plan
        if task.get("kind", "development") != "development":
            raise HiveError("workplan requires a development task")
        validate_plan(task["workplan"])
    if "dispatches" in task:
        from .dispatch import validate_dispatches
        validate_dispatches(task)


def _read(task_id: str, directory: Path) -> dict[str, Any]:
    path = _path(task_id, directory)
    if path.is_symlink():
        raise HiveError("Hive state must not be a symlink")
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HiveError(f"task not found: {task_id}") from exc
    except (ValueError, OSError) as exc:
        raise HiveError(f"unreadable Hive task: {task_id}") from exc
    if not isinstance(task, dict) or task.get("id") != task_id:
        raise HiveError("task id does not match state filename")
    # v1 did not record deployment. Reading it never invents a release receipt.
    if "schema_version" not in task:
        if task.get("size") not in {"S", "M", "L"} or not isinstance(task.get("nodes"), list):
            raise HiveError("invalid legacy task schema")
        task["schema_version"] = 2
        task["revision"] = 0
        task["history"] = [{"event": "legacy_import", "at": _now()}]
        task["nodes"].append(_nodes(task["size"])[-1])
        task["status"] = "active"
    validate_task(task)
    return task


def _save(task: dict[str, Any], directory: Path) -> None:
    """Caller owns locked_task. Atomic replacement is not a substitute for locking."""
    validate_task(task)
    task["updated_at"] = _now()
    task["revision"] += 1
    states = {node["status"] for node in task["nodes"]}
    if states & STOPPED or task.get("execution", {}).get("status") in STOPPED:
        task["status"] = "blocked"
    elif all(node["status"] in DONE for node in task["nodes"]):
        # A completed development DAG is a necessary workflow condition, never
        # proof that the registered code was integrated and released.
        from .completion import completion
        complete, _missing = completion(task, directory=directory)
        task["status"] = "complete" if complete else "active"
    elif any(node["id"] == "integrate" and node["status"] == "succeeded" for node in task["nodes"]):
        task["status"] = "integrated"
    else:
        task["status"] = "active"
    target = _path(task["id"], directory)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(task, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def create_task(goal: str, *, size: str = "M", state_dir: Path | None = None,
                source_key: str = "", kind: str = "development") -> dict[str, Any]:
    if not isinstance(goal, str) or not goal.strip():
        raise HiveError("goal is required")
    size = size.upper()
    if size not in {"S", "M", "L"}:
        raise HiveError("size must be S, M, or L")
    if kind not in {"development", "analysis"}:
        raise HiveError("kind must be development or analysis")
    if not isinstance(source_key, str):
        raise HiveError("invalid source_key")
    source_key = source_key.strip()
    if source_key:
        task_id = "HIVE-" + hashlib.sha256(source_key.encode()).hexdigest()[:32].upper()
    else:
        task_id = f"HIVE-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(8).upper()}"
    with locked_task(task_id, state_dir) as directory:
        if _path(task_id, directory).exists():
            task = _read(task_id, directory)
            if not source_key or task.get("source_key") != source_key:
                raise HiveError("task id collision")
            if task["size"] != size:
                raise HiveError("source_key already has a different task size; explicit scope migration required")
            if task.get("kind", "development") != kind:
                raise HiveError("source_key already has a different task kind; explicit workflow promotion required")
            return task
        task = {"schema_version": 2, "revision": 0, "id": task_id, "source_key": source_key,
                "goal": goal.strip(), "size": size, "kind": kind, "status": "active", "created_at": _now(),
                "nodes": _nodes(size, kind), "bindings": [], "history": [{"event": "created", "at": _now()}]}
        _save(task, directory)
        return task


def load_task(task_id: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    # Writers replace a complete JSON file. Reads do not create directories/locks.
    return _read(task_id, state_directory(state_dir))


def attach_binding(task_id: str, *, system: str, external_id: str, node_id: str,
                   state_dir: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_-]+", system) or not external_id.strip():
        raise HiveError("invalid external binding")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        if node_id not in {node["id"] for node in task["nodes"]}:
            raise HiveError(f"node not found: {node_id}")
        binding = {"system": system, "external_id": external_id.strip(), "node_id": node_id}
        bindings = task.setdefault("bindings", [])
        if binding not in bindings:
            bindings.append(binding)
            _save(task, directory)
        return task


def _invalidate(task: dict, node_id: str) -> None:
    affected = {node_id}
    for node in task["nodes"]:
        if node["id"] in affected or affected.intersection(node["depends_on"]):
            affected.add(node["id"])
            if node["id"] == node_id:
                continue
            if node["status"] != "queued" or node["artifacts"]:
                task["history"].append({"event": "invalidated", "at": _now(), "by_node": node_id,
                                        "previous": deepcopy(node)})
            node.update(status="queued", owner=None, artifacts=[], updated_at=_now())
            node.pop("approved_at", None)


def _mark(task: dict, node_id: str, status: str, *, owner: str, artifact: str,
          approved: bool, retry: bool = False, delivery_write: bool = False) -> None:
    node = next((n for n in task["nodes"] if n["id"] == node_id), None)
    if node is None:
        raise HiveError(f"node not found: {node_id}")
    if node_id == "implement" and status == "succeeded" and "workplan" in task:
        unfinished = [p["id"] for p in task["workplan"]["packages"] if p["status"] != "succeeded"]
        if unfinished:
            raise HiveError("implementation requires completed work packages: " + ",".join(unfinished))
    if status not in STATUSES:
        raise HiveError("invalid node status")
    if not owner.strip():
        raise HiveError("active or completed node requires an owner")
    if node["owner"] and node["owner"] != owner and node["status"] in ACTIVE | DONE:
        raise HiveError("node already leased or completed by another owner")
    if node["owner"] and node["owner"] != owner and node["status"] in STOPPED and not retry:
        raise HiveError("stopped node requires explicit retry before changing owner")
    if node["status"] in STOPPED and status not in STOPPED and not retry:
        raise HiveError("stopped node requires explicit retry")
    if status == "queued" and not retry:
        raise HiveError("use retry to requeue a node")
    if retry and node["status"] in ACTIVE:
        raise HiveError("interrupt the active node before retry")
    if status == "skipped" and (node_id != "research" or not approved or not artifact.strip()):
        raise HiveError("only research may be explicitly waived with evidence")
    if status in ACTIVE | DONE:
        blockers = [n["id"] for n in task["nodes"] if n["id"] in node["depends_on"] and n["status"] not in DONE]
        if blockers:
            raise HiveError("dependencies incomplete: " + ",".join(blockers))
    if node_id == "review" and status == "succeeded" and task.get("kind") == "analysis":
        plan = next(n for n in task["nodes"] if n["id"] == "plan")
        if plan["owner"].strip().casefold() == owner.strip().casefold():
            raise HiveError("reviewer must differ from planner")
        if not artifact.strip() or any(not n["artifacts"] for n in task["nodes"] if n["id"] != "review"):
            raise HiveError("analysis completion requires evidence for all nodes")
    if node_id in {"integrate", "release"} and status in ACTIVE | DONE:
        if not approved:
            raise HiveError("high-risk node requires explicit approval")
        from .guard import validate
        validate(task, phase=node_id)
        if status == "succeeded" and not artifact.strip():
            raise HiveError("integration/release requires an evidence artifact")
    if node_id in {"integrate", "release"} and status == "succeeded" and not delivery_write:
        raise HiveError("integration/release success is reserved for a delivery receipt transaction")
    previous = deepcopy(node)
    changed = status != node["status"] or (artifact and artifact not in node["artifacts"])
    if not changed:
        return
    if node["status"] in DONE or status in STOPPED or retry:
        _invalidate(task, node_id)
        node["artifacts"] = []
        node.pop("approved_at", None)
    node.update(status=status, owner=None if retry else owner, updated_at=_now())
    if artifact.strip() and not retry and artifact not in node["artifacts"]:
        node["artifacts"].append(artifact)
    if approved and status in DONE:
        node["approved_at"] = _now()
    task["history"].append({"event": "retry" if retry else "node", "at": _now(), "node_id": node_id,
                            "owner": owner, "reason": artifact, "previous": previous, "status": status})


def mark_node(task_id: str, node_id: str, status: str, *, owner: str = "", artifact: str = "",
              approved: bool = False, state_dir: Path | None = None) -> dict[str, Any]:
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        _mark(task, node_id, status, owner=owner, artifact=artifact, approved=approved)
        _save(task, directory)
        return task


def retry_node(task_id: str, node_id: str, *, owner: str, reason: str,
               state_dir: Path | None = None) -> dict[str, Any]:
    if not reason.strip():
        raise HiveError("retry requires a reason")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        _mark(task, node_id, "queued", owner=owner, artifact=reason, approved=False, retry=True)
        _recover_peer_review_executors(task, node_id, owner=owner, reason=reason)
        _save(task, directory)
        return task


def _recover_peer_review_executors(task: dict, node_id: str, *, owner: str, reason: str) -> None:
    """An explicit review retry also reopens its rejected executor receipt.

    Called only by operator retry entrypoints, never by ordinary node changes.
    Preserve the failed receipt in history and retain all iteration/budget limits.
    Test/review loop exhaustion is a separate stop, not a peer-identity error.
    """
    if node_id != "review":
        return
    for run_id, executor in task.get("executors", {}).items():
        if not executor.get("blocked") or not str(executor.get("block_reason") or "").startswith("peer_review: "):
            continue
        phases = executor.get("phases", {})
        previous = {"blocked": executor["blocked"], "block_reason": executor["block_reason"],
                    "phases": {phase: deepcopy(phases.get(phase, {})) for phase in ("reviewer", "integrator")}}
        event = {"event": "explicit_peer_review_retry", "at": _now(), "owner": owner,
                 "reason": reason, "run_id": run_id, "previous": previous}
        executor.setdefault("history", []).append(deepcopy(event))
        executor.update(blocked=False, block_reason=None, updated_at=_now())
        # The rejected verdict is not approval, but previously spent review
        # rounds remain spent. An identity correction cannot extend the budget.
        review_rounds = phases.get("reviewer", {}).get("review_rounds")
        phases["reviewer"] = {"review_rounds": review_rounds} if review_rounds is not None else {}
        phases["integrator"] = {}
        task["history"].append(event)
