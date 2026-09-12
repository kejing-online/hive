"""Decaying shared field. Bees read marks on paths; they do not invent DAG stages.

Kinds:
  need        queen laid work
  busy        a worker holds the slice
  done        a worker finished writing
  unverified  written, no soldier has consumed it
  verified    soldier-verify passed
  reviewed    soldier-review passed
  alarm       failure; others keep away
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .state import HiveError

KINDS = ("need", "busy", "done", "unverified", "verified", "reviewed", "alarm")
EPSILON = 0.05
DEFAULT_HALF_LIFE = 300.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HiveError("scent timestamp must be timezone-aware ISO-8601") from exc
    if value.tzinfo is None:
        raise HiveError("scent timestamp must be timezone-aware ISO-8601")
    return value


def intensity(mark: dict[str, Any], *, now: datetime | None = None, half_life: float = DEFAULT_HALF_LIFE) -> float:
    if half_life <= 0:
        raise HiveError("scent half_life_seconds must be positive")
    age = ((now or _now()) - _parse(mark["at"])).total_seconds()
    if age < 0:
        age = 0.0
    base = float(mark.get("intensity") or 0)
    return base * (0.5 ** (age / half_life))


def _blank() -> dict[str, Any]:
    return {"schema_version": 1, "half_life_seconds": DEFAULT_HALF_LIFE, "marks": []}


def load(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("scent")
    if raw is None:
        return _blank()
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise HiveError("invalid scent field")
    marks = raw.get("marks")
    if not isinstance(marks, list):
        raise HiveError("invalid scent field")
    half = raw.get("half_life_seconds", DEFAULT_HALF_LIFE)
    if not isinstance(half, (int, float)) or isinstance(half, bool) or half <= 0:
        raise HiveError("scent half_life_seconds must be positive")
    return {"schema_version": 1, "half_life_seconds": float(half), "marks": list(marks)}


def field(task: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Live marks with current intensity; evaporated traces are dropped."""
    payload = load(task)
    now = now or _now()
    live = []
    for mark in payload["marks"]:
        if not isinstance(mark, dict) or mark.get("kind") not in KINDS:
            continue
        current = intensity(mark, now=now, half_life=payload["half_life_seconds"])
        if current < EPSILON:
            continue
        live.append({**mark, "intensity": round(current, 4)})
    return live


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def strength(live: list[dict[str, Any]], kind: str, paths: list[str]) -> float:
    total = 0.0
    for mark in live:
        if mark.get("kind") != kind:
            continue
        if any(_overlap(str(mark.get("path") or ""), path) for path in paths):
            total += float(mark["intensity"])
    return total


def _prune(marks: list[Any], *, half_life: float, now: datetime) -> list[Any]:
    """Drop marks that decayed below EPSILON so persisted fields stay bounded."""
    kept = []
    for mark in marks:
        if not isinstance(mark, dict) or mark.get("kind") not in KINDS:
            continue
        try:
            current = intensity(mark, now=now, half_life=half_life)
        except HiveError:
            continue
        if current >= EPSILON:
            kept.append(mark)
    return kept


def deposit(task: dict[str, Any], *, path: str, kind: str, by: str, package_id: str = "",
            amount: float = 1.0, now: datetime | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise HiveError("unknown scent kind")
    if not isinstance(path, str) or not path.strip() or path.startswith("/"):
        raise HiveError("scent path must be a relative path")
    if not isinstance(by, str) or not by.strip():
        raise HiveError("scent depositor is required")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        raise HiveError("scent intensity must be positive")
    stamp = now or _now()
    if stamp.tzinfo is None:
        raise HiveError("scent timestamp must be timezone-aware ISO-8601")
    payload = load(task)
    payload["marks"] = _prune(payload["marks"], half_life=payload["half_life_seconds"], now=stamp)
    payload["marks"].append({
        "path": path.strip(),
        "kind": kind,
        "intensity": float(amount),
        "at": stamp.isoformat(),
        "by": by.strip(),
        "package_id": package_id,
    })
    task["scent"] = payload
    return payload


def _ancestors(path: str) -> list[str]:
    parts = [part for part in str(path).split("/") if part and part != "."]
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def map_field(task: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Net intensity per path. Neighbourhood view, not a chat log."""
    buckets: dict[str, dict[str, float]] = {}
    for mark in field(task, now=now):
        slot = buckets.setdefault(str(mark.get("path") or ""), {kind: 0.0 for kind in KINDS})
        kind = str(mark.get("kind") or "")
        if kind in slot:
            slot[kind] += float(mark["intensity"])
    rows = []
    for path, kinds in sorted(buckets.items()):
        row = {"path": path}
        for kind, value in kinds.items():
            if value:
                row[kind] = round(value, 4)
        rows.append(row)
    return rows


def evaporate(task: dict[str, Any], *, paths: list[str], kinds: tuple[str, ...] | list[str]) -> None:
    payload = load(task)
    kept = []
    for mark in payload["marks"]:
        if mark.get("kind") in kinds and any(_overlap(str(mark.get("path") or ""), path) for path in paths):
            continue
        kept.append(mark)
    payload["marks"] = kept
    task["scent"] = payload


def on_queen_plan(task: dict[str, Any], packages: list[dict[str, Any]], *, by: str) -> None:
    for package in packages:
        if not str(package.get("id") or "").startswith("worker-"):
            continue
        for path in package.get("write_paths") or []:
            deposit(task, path=path, kind="need", by=by, package_id=str(package["id"]))


def on_claim(task: dict[str, Any], package: dict[str, Any], *, by: str) -> None:
    paths = list(package.get("write_paths") or [])
    evaporate(task, paths=paths, kinds=("need",))
    for path in paths:
        deposit(task, path=path, kind="busy", by=by, package_id=str(package.get("id") or ""))


def on_finish(task: dict[str, Any], package: dict[str, Any], *, by: str) -> None:
    ident = str(package.get("id") or "")
    writes = list(package.get("write_paths") or [])
    reads = list(package.get("read_paths") or [])
    evaporate(task, paths=writes, kinds=("busy", "need", "alarm"))
    if ident.startswith("soldier-verify"):
        evaporate(task, paths=reads + writes, kinds=("unverified",))
        for path in reads + writes:
            deposit(task, path=path, kind="verified", by=by, package_id=ident)
        return
    if ident.startswith("soldier-review"):
        for path in reads + writes:
            deposit(task, path=path, kind="reviewed", by=by, package_id=ident)
        return
    for path in writes:
        deposit(task, path=path, kind="done", by=by, package_id=ident)
        deposit(task, path=path, kind="unverified", by=by, package_id=ident)
        for parent in _ancestors(path):
            deposit(task, path=parent, kind="unverified", by=by, package_id=ident, amount=0.5)
    _recruit(task, package, by=by)


def on_fail(task: dict[str, Any], package: dict[str, Any], *, by: str) -> None:
    paths = list(package.get("write_paths") or []) + list(package.get("read_paths") or [])
    evaporate(task, paths=paths, kinds=("busy",))
    ident = str(package.get("id") or "")
    for path in package.get("write_paths") or []:
        deposit(task, path=path, kind="alarm", by=by, package_id=ident)
        for parent in _ancestors(path):
            deposit(task, path=parent, kind="alarm", by=by, package_id=ident, amount=0.5)


def _recruit(task: dict[str, Any], finished: dict[str, Any], *, by: str) -> None:
    """Waggle analog: a finished worker boosts need on still-queued sibling slices."""
    plan = task.get("workplan") or {}
    finished_id = str(finished.get("id") or "")
    if not finished_id.startswith("worker-"):
        return
    for other in plan.get("packages") or []:
        if not isinstance(other, dict):
            continue
        if str(other.get("id") or "") == finished_id:
            continue
        if not str(other.get("id") or "").startswith("worker-"):
            continue
        if other.get("status") != "queued":
            continue
        for path in other.get("write_paths") or []:
            deposit(task, path=path, kind="need", by=by, package_id=finished_id, amount=0.4)


def attraction(package: dict[str, Any], live: list[dict[str, Any]]) -> float:
    ident = str(package.get("id") or "")
    writes = list(package.get("write_paths") or [])
    reads = list(package.get("read_paths") or [])
    alarm = strength(live, "alarm", writes + reads)
    busy = strength(live, "busy", writes + reads)
    if ident.startswith("soldier-"):
        return strength(live, "unverified", reads) - 2 * busy - 2 * alarm
    return strength(live, "need", writes) - 2 * busy - 2 * alarm
