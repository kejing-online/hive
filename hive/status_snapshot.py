"""Read existing Hive and team projections with explicit source timestamps.

No task transitions, queue recovery, scheduler invocation, or production writes.
Stale/missing/broken sources remain visible and never masquerade as empty work.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from .completion import completion


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("symlink source")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("source is not an object")
    return value


def _source(path: Path, fields: tuple[str, ...], now: datetime, max_age: float) -> dict:
    try:
        value = _read(path)
        stamp = next((value.get(key) for key in ("observed_at", "checked_at", "last_tick", "last", "finished_at")
                      if value.get(key)), None)
        stamp_kind = "payload" if stamp else "file_mtime"
        observed = (datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp
                    else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        if observed.tzinfo is None:
            raise ValueError("source timestamp lacks timezone")
        age = (now - observed).total_seconds()
        return {"freshness": "clock_skew" if age < -60 else ("stale" if age > max_age else "fresh"),
                "observed_at": observed.isoformat(), "timestamp_source": stamp_kind,
                "age_seconds": round(age, 1), "data": {key: value[key] for key in fields if key in value}}
    except FileNotFoundError:
        return {"freshness": "missing", "path": str(path)}
    except (OSError, ValueError, TypeError) as exc:
        return {"freshness": "error", "path": str(path), "error_code": type(exc).__name__}


def snapshot(home: Path | None = None, max_age_seconds: float = 900, *,
             now: datetime | None = None, log_dir: Path | None = None) -> dict:
    """Join source projections without overwriting their independent meanings."""
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    home = Path(home) if home is not None else Path.home() / ".hive"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("observation time requires timezone")
    sources = {}
    for name, path, fields in (
        ("factory", home / "factory_status.json", ("result", "counts")),
        ("worker", home / "worker_status.json", ("result", "skip_reason")),
        ("intake", home / "intake_status.json", ("sources", "degraded")),
        ("runtime", home / "runtime/current.json", ("sha", "hive_task_ids", "runtime_sha", "release", "degraded")),
        ("launcher", (log_dir or (Path.home() / ".hive" / "logs")) / "auto_merge_status.json",
         ("phase", "requested_sha", "detail", "hive_exit_code", "release_complete")),
    ):
        sources[name] = _source(path, fields, now, max_age_seconds)

    directory = home / "hive"
    counts, verified, unverified, errors = Counter(), 0, [], []
    for path in sorted(directory.glob("HIVE-*.json")):
        try:
            task = _read(path)
            counts[str(task.get("status") or "unknown")] += 1
            complete, missing = completion(task, directory=directory)
            verified += int(complete)
            if task.get("status") == "complete" and not complete:
                unverified.append({"id": task.get("id", path.stem), "missing": missing})
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            errors.append(path.name)
    sources["hive"] = {"freshness": "error" if errors else ("fresh" if directory.is_dir() else "missing"),
                       "observed_at": now.isoformat(), "raw_status_counts": dict(counts),
                       "verified_complete": verified, "unverified_complete": unverified, "read_errors": errors}
    queue_dir = home / "queue"
    queue_counts, bound, errors = Counter(), 0, []
    for path in sorted(queue_dir.glob("*.json")):
        try:
            ticket = _read(path)
            queue_counts[str(ticket.get("status") or "unknown")] += 1
            bound += int(bool(ticket.get("hive_task_id")))
        except (OSError, ValueError, TypeError):
            errors.append(path.name)
    sources["queue"] = {"freshness": "error" if errors else ("fresh" if queue_dir.is_dir() else "missing"),
                        "observed_at": now.isoformat(), "status_counts": dict(queue_counts),
                        "hive_bound": bound, "read_errors": errors}
    return {"observed_at": now.isoformat(), "max_age_seconds": max_age_seconds,
            "sources": sources, "read_only": True}
