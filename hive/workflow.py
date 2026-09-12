"""Whole-task executor leases shared by team cards and factory tickets.

Executors own a run, not every DAG node. Completion consumes actual Hive evidence;
handoff transfers the same task. Domain errors fail closed and are replay-safe.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .guard import validate
from .state import ACTIVE, STOPPED, HiveError, _mark, _nodes, _now, _read, _save, _recover_peer_review_executors, locked_task


def _binding(task: dict, system: str, external_id: str) -> dict:
    if not system or not external_id:
        raise HiveError("workflow binding requires system and external id")
    binding = {"system": system, "external_id": external_id, "node_id": "workflow"}
    if binding not in task.get("bindings", []):
        raise HiveError("workflow has no matching executor binding")
    return binding


def bind(task_id: str, external_id: str, *, system: str = "team", state_dir: Path) -> None:
    if not system or not external_id:
        raise HiveError("workflow binding requires system and external id")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        binding = {"system": system, "external_id": external_id, "node_id": "workflow"}
        if binding not in task["bindings"]:
            task["bindings"].append(binding)
            _save(task, directory)


def claim(task_id: str, external_id: str, run_id: str, *, system: str = "team", state_dir: Path) -> None:
    if not isinstance(run_id, str) or not run_id.strip():
        raise HiveError("workflow requires a run id")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        _binding(task, system, external_id)
        lease = task.get("execution", {})
        identity = {"system": system, "external_id": external_id, "run_id": run_id}
        if lease.get("status") in ACTIVE:
            if all(lease.get(key) == value for key, value in identity.items()):
                return  # Exact pending-claim replay only.
            raise HiveError("workflow is already leased by another executor")
        if lease.get("status") in STOPPED:
            raise HiveError("workflow requires explicit retry")
        retry_ack = lease.get("status") == "queued" and lease.get("system") == system and lease.get("external_id") == external_id
        if task["status"] == "complete" and not retry_ack:
            raise HiveError("workflow already complete; no duplicate execution")
        task["execution"] = {**identity, "status": "leased", "at": _now()}
        intake = task["nodes"][0]
        if intake["status"] == "queued":
            _mark(task, "intake", "succeeded", owner=f"{system}:{run_id}",
                  artifact=f"task:{external_id}: {task['goal']}", approved=False)
        task["history"].append({"event": "workflow_claim", **task["execution"]})
        _save(task, directory)


def _promote(task: dict, *, reason: str) -> None:
    """Only called inside a verified planning handoff, in the same transaction."""
    if task.get("kind", "development") != "analysis":
        return
    previous = deepcopy(task["nodes"])
    preserved = {node["id"]: node for node in task["nodes"] if node["id"] != "review"}
    task["kind"] = "development"
    task["nodes"] = [{**node, **preserved.get(node["id"], {}), "depends_on": node["depends_on"]}
                     for node in _nodes(task["size"])]
    task["history"].append({"event": "workflow_promoted", "at": _now(), "reason": reason,
                            "previous_nodes": previous})
    # An analysis executor's review cannot approve code that does not yet exist.
    for executor in task.get("executors", {}).values():
        executor["meta"]["kind"] = "development"
        executor["required_phases"] = ["planner", "implementer", "tester", "reviewer"]
        if task["size"] == "L":
            executor["required_phases"].append("security")
        executor["required_phases"].append("integrator")
        for phase in executor["phases"]:
            if phase != "planner":
                executor["phases"][phase] = {}
        executor["history"].append({"event": "workflow_promoted", "at": _now(), "reason": reason})


def convert_queued_kind(task_id: str, kind: str, *, reason: str, state_dir: Path) -> dict:
    """Explicit migration of a never-executed intake; not a completed-task downgrade."""
    if kind not in {"analysis", "development"} or not isinstance(reason, str) or not reason.strip():
        raise HiveError("queued kind conversion requires a valid kind and audit reason")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        if task.get("kind", "development") == kind:
            return task
        if task.get("execution") or task.get("executors") or task.get("delivery") or any(
            n["status"] != "queued" or n["owner"] or n["artifacts"] for n in task["nodes"]
        ):
            raise HiveError("kind conversion requires a never-executed queued task")
        previous = task.get("kind", "development")
        target_ids = {n["id"] for n in _nodes(task["size"], kind)} | {"workflow"}
        if any(binding.get("node_id") not in target_ids for binding in task.get("bindings", [])):
            raise HiveError("kind conversion would orphan an existing node binding")
        task["kind"] = kind
        task["nodes"] = _nodes(task["size"], kind)
        task["history"].append({"event": "queued_kind_converted", "from": previous, "to": kind,
                                "reason": reason.strip(), "at": _now()})
        _save(task, directory)
        return task


def settle(task_id: str, external_id: str, run_id: str, status: str, artifact: str, *,
           handoff: dict | None = None, system: str = "team", state_dir: Path) -> None:
    mapped = "succeeded" if status in {"done", "completed"} else status
    if mapped not in {"succeeded", *STOPPED}:
        raise HiveError("invalid workflow settlement")
    if mapped == "succeeded" and not artifact.strip():
        raise HiveError("workflow completion requires evidence")
    if handoff is not None and (mapped != "succeeded" or not isinstance(handoff, dict) or not handoff.get("group")):
        raise HiveError("workflow handoff requires a successful run and target group")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        _binding(task, system, external_id)
        lease = task.get("execution", {})
        if any(lease.get(key) != value for key, value in
               {"system": system, "external_id": external_id, "run_id": run_id}.items()):
            raise HiveError("workflow receipt does not own the lease")
        receipt = {"status": mapped, "artifact": artifact, "handoff": handoff}
        if lease.get("status") not in ACTIVE:
            if all(lease.get(key) == value for key, value in receipt.items()):
                return
            raise HiveError("workflow receipt conflicts with existing terminal receipt")
        if mapped == "succeeded":
            if handoff and any(node["status"] in ACTIVE for node in task["nodes"]):
                raise HiveError("workflow handoff requires all node workers to finish or stop")
            if handoff is None or task.get("kind") == "analysis":
                validate(task, phase="complete", state_dir=directory)
            if handoff and handoff["group"] != "planning":
                _promote(task, reason=artifact)
        else:
            # Preserve completed steps; stop only the live work. A retry must
            # explicitly requeue failed nodes before new owners can claim them.
            for node in task["nodes"]:
                if node["status"] in ACTIVE:
                    _mark(task, node["id"], mapped, owner=node["owner"], artifact=artifact, approved=False)
        lease.update(receipt, at=_now())
        task["history"].append({"event": "workflow_settle", **deepcopy(lease)})
        _save(task, directory)


def retry(task_id: str, external_id: str, *, reason: str, system: str = "team", state_dir: Path) -> None:
    if not isinstance(reason, str) or not reason.strip():
        raise HiveError("workflow retry requires a reason")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        _binding(task, system, external_id)
        lease = task.get("execution", {})
        if lease.get("system") != system or lease.get("external_id") != external_id or lease.get("status") not in STOPPED:
            raise HiveError("workflow retry requires this executor's stopped lease")
        for node in task["nodes"]:
            if node["status"] in STOPPED:
                _mark(task, node["id"], "queued", owner=f"{system}:retry:{external_id}",
                      artifact=reason, approved=False, retry=True)
                _recover_peer_review_executors(task, node["id"], owner=f"{system}:retry:{external_id}", reason=reason)
        task["history"].append({"event": "workflow_retry", "previous": deepcopy(lease), "reason": reason, "at": _now()})
        task["execution"] = {"system": system, "external_id": external_id, "status": "queued"}
        _save(task, directory)
