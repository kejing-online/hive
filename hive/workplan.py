"""Pure validation and scheduling for an optional Hive work-package graph.

This module deliberately has no state or execution dependencies: callers persist
leases and transitions themselves, then pass the resulting plan back here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import posixpath
import re
from typing import Any

from .state import HiveError


_STATUSES = {"queued", "running", "succeeded", "failed", "interrupted"}
_RUNTIME_FIELDS = {"status", "attempts", "lease"}
_PACKAGE_FIELDS = {
    "id", "title", "depends_on", "read_paths", "write_paths", "acceptance",
    "priority", "max_attempts", *_RUNTIME_FIELDS,
}
_SPEC_PACKAGE_FIELDS = _PACKAGE_FIELDS - _RUNTIME_FIELDS
_PLAN_FIELDS = {"schema_version", "max_parallel", "packages"}
_PERSISTED_PACKAGE_FIELDS = _PACKAGE_FIELDS | {"artifact", "failure_reason", "interruption_reason"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _path(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise HiveError(f"invalid {label} path")
    if "\x00" in value or any(char in value for char in "*?[]{}"):
        raise HiveError(f"invalid {label} path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HiveError(f"invalid {label} path")


def _paths(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise HiveError(f"{label}_paths must be a list")
    if not all(isinstance(path, str) for path in value):
        raise HiveError(f"invalid {label} path")
    if len(set(value)) != len(value):
        raise HiveError(f"duplicate {label}_paths")
    for path in value:
        _path(path, label)


def _packages(plan: dict[str, Any]) -> list[dict[str, Any]]:
    packages = plan.get("packages")
    if not isinstance(packages, list) or not packages:
        raise HiveError("packages must be a nonempty list")
    if not all(isinstance(package, dict) for package in packages):
        raise HiveError("package must be an object")
    return packages


def validate_plan(plan: dict[str, Any]) -> None:
    """Validate a complete, persisted package plan and its dependency DAG."""
    if not isinstance(plan, dict) or not _is_int(plan.get("schema_version")) or plan.get("schema_version") != 1:
        raise HiveError("invalid work plan schema")
    if set(plan) - _PLAN_FIELDS:
        raise HiveError("work plan has unknown fields")
    if not _is_int(plan.get("max_parallel")) or not 1 <= plan["max_parallel"] <= 32:
        raise HiveError("max_parallel must be an integer from 1 through 32")
    packages = _packages(plan)
    ids: set[str] = set()
    for package in packages:
        if not _PACKAGE_FIELDS.issubset(package):
            raise HiveError("package has missing required fields")
        if set(package) - _PERSISTED_PACKAGE_FIELDS:
            raise HiveError("package has unknown fields")
        identifier = package["id"]
        if not isinstance(identifier, str) or not identifier.strip():
            raise HiveError("package ids must be unique nonempty strings")
        if identifier in ids:
            raise HiveError("package ids must be unique nonempty strings")
        ids.add(identifier)
        if not isinstance(package["title"], str) or not package["title"].strip():
            raise HiveError("package title must be a nonempty string")
        deps = package["depends_on"]
        if not isinstance(deps, list) or not all(isinstance(dep, str) and dep for dep in deps) or len(set(deps)) != len(deps):
            raise HiveError("depends_on must contain unique nonempty ids")
        _paths(package["read_paths"], "read")
        _paths(package["write_paths"], "write")
        if not package["read_paths"] and not package["write_paths"]:
            raise HiveError("package must have a read or write path")
        acceptance = package["acceptance"]
        if not isinstance(acceptance, list) or not acceptance or not all(isinstance(item, str) and item.strip() for item in acceptance):
            raise HiveError("acceptance must contain nonempty strings")
        if not _is_int(package["priority"]) or not 0 <= package["priority"] <= 100:
            raise HiveError("priority must be an integer from 0 through 100")
        if not _is_int(package["max_attempts"]) or not 1 <= package["max_attempts"] <= 5:
            raise HiveError("max_attempts must be an integer from 1 through 5")
        if not isinstance(package["status"], str) or package["status"] not in _STATUSES or not _is_int(package["attempts"]) or not 0 <= package["attempts"] <= package["max_attempts"]:
            raise HiveError("invalid package runtime state")
        if package["status"] != "queued" and package["attempts"] < 1:
            raise HiveError("started package requires an attempt")
        lease = package["lease"]
        if package["status"] != "running" and lease is not None:
            raise HiveError("only running package can retain a lease")
        if package["status"] == "running":
            if not isinstance(lease, dict) or not all(isinstance(lease.get(key), str) and lease[key].strip() for key in ("token", "owner", "expires_at")):
                raise HiveError("running package requires token, owner and expires_at lease")
            try:
                if datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
            except ValueError as exc:
                raise HiveError("running lease expires_at must be timezone-aware") from exc
        artifact = package.get("artifact")
        if artifact is not None:
            if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}
                    or not isinstance(artifact["path"], str) or not artifact["path"].startswith("/")
                    or "\x00" in artifact["path"] or posixpath.normpath(artifact["path"]) != artifact["path"]
                    or not isinstance(artifact["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])):
                raise HiveError("artifact must contain path and sha256")
        for field in ("failure_reason", "interruption_reason"):
            if field in package and (not isinstance(package[field], str) or not package[field].strip()):
                raise HiveError(f"{field} must be a nonempty string")
    by_id = {package["id"]: package for package in packages}
    for package in packages:
        for dep in package["depends_on"]:
            if dep not in by_id:
                raise HiveError(f"missing dependency: {dep}")
            if dep == package["id"]:
                raise HiveError("package cannot depend on itself")
    # Kahn's algorithm avoids recursion limits for large, generated plans.
    indegree = {identifier: len(package["depends_on"]) for identifier, package in by_id.items()}
    dependents = {identifier: [] for identifier in by_id}
    for identifier, package in by_id.items():
        for dep in package["depends_on"]:
            dependents[dep].append(identifier)
    queue = [identifier for identifier, count in indegree.items() if count == 0]
    seen = 0
    while queue:
        identifier = queue.pop()
        seen += 1
        for dependent in dependents[identifier]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if seen != len(by_id):
        raise HiveError("package dependency graph contains a cycle")


def normalize_plan(spec: dict[str, Any]) -> dict[str, Any]:
    """Copy a static package specification and add only default runtime fields."""
    if not isinstance(spec, dict):
        raise HiveError("work plan spec must be an object")
    copied = deepcopy(spec)
    if set(copied) - _PLAN_FIELDS:
        raise HiveError("work plan spec has unknown fields")
    if "schema_version" in copied and (not _is_int(copied["schema_version"]) or copied["schema_version"] != 1):
        raise HiveError("invalid work plan schema")
    copied["schema_version"] = 1
    packages = _packages(copied)
    for package in packages:
        if set(package) - _SPEC_PACKAGE_FIELDS:
            raise HiveError("work plan spec cannot set runtime state")
        package["status"] = "queued"
        package["attempts"] = 0
        package["lease"] = None
    validate_plan(copied)
    return copied


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _conflicts(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Read/read access is compatible. Every other overlapping pair is exclusive.
    return any(_overlap(a, b) for a in left["write_paths"] for b in (right["read_paths"] + right["write_paths"])) or any(
        _overlap(a, b) for a in left["read_paths"] for b in right["write_paths"]
    )


def select_ready(plan: dict[str, Any], *, capacity: int | None = None,
                 scent_field: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return schedulable queued package ids without changing ``plan``.

    DAG edges and path locks stay law. When ``scent_field`` is present, ready
    packages are ordered by attraction, and soldiers may follow unverified
    traces even if a dependency has not been marked succeeded.
    """
    validate_plan(plan)
    if capacity is not None and (not _is_int(capacity) or capacity < 0):
        raise HiveError("capacity must be a nonnegative integer or null")
    limit = plan["max_parallel"] if capacity is None else min(plan["max_parallel"], capacity)
    packages = plan["packages"]
    by_id = {package["id"]: package for package in packages}
    active = [package["id"] for package in packages if package["status"] == "running"]
    available = max(0, limit - len(active))
    blocked: dict[str, list[str]] = {}
    candidates: list[tuple[float, int, int, dict[str, Any]]] = []
    live = list(scent_field or [])
    from .scent import attraction, soldier_can_follow_scent
    for index, package in enumerate(packages):
        if package["status"] != "queued":
            if package["status"] != "running":
                blocked[package["id"]] = [f"status is {package['status']}"]
            continue
        reasons = [f"dependency not succeeded: {dep}" for dep in package["depends_on"] if by_id[dep]["status"] != "succeeded"]
        if reasons and live and soldier_can_follow_scent(package, live):
            reasons = [row for row in reasons if not row.startswith("dependency not succeeded")]
        if package["attempts"] >= package["max_attempts"]:
            reasons.append("attempt budget exhausted")
        if reasons:
            blocked[package["id"]] = reasons
        else:
            score = attraction(package, live) if live else 0.0
            candidates.append((-score, -package["priority"], index, package))
    ready: list[str] = []
    reserved = [package for package in packages if package["status"] == "running"]
    for _, _, _, package in sorted(candidates):
        identifier = package["id"]
        if len(ready) >= available:
            blocked[identifier] = ["capacity exhausted"]
        else:
            conflict = next((other["id"] for other in reserved if _conflicts(package, other)), None)
            if conflict is not None:
                blocked[identifier] = [f"path conflict with {conflict}"]
            else:
                ready.append(identifier)
                reserved.append(package)
    return {"ready": ready, "blocked": blocked, "active": active}
