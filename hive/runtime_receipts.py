"""Observe a successful immutable development runtime, never a business-task result.

Invoked by the installed runtime bootstrap with its factual status JSON on stdin.
Failure exits nonzero and leaves the Hive release incomplete; retry is receipt-only.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import sys

from .delivery import _git, guard_ref, released, resolve_ref
from .state import HiveError, load_task, state_directory
from .evidence import write_artifact
from .receipt_validation import validate_release_receipt


def _released_generation_replay(sha: str, directory: Path) -> list[dict] | None:
    """Replay a factual receipt from an immutable pre-rework generation.

    A reworked task's current delivery is intentionally unintegrated, so the
    normal release guard must reject it.  The runtime may nevertheless be
    running that task's last released checkout as a safe fallback; in that
    case only the already-recorded receipt is replayed, never a new release is
    minted for the stale generation.
    """
    for path in sorted(directory.glob("HIVE-*.json")):
        if path.is_symlink():
            continue
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for generation in (task.get("delivery_generations") if isinstance(task, dict) else []) or []:
            if not isinstance(generation, dict):
                continue
            integrated = generation.get("integrated")
            released_receipt = generation.get("released")
            receipt = (generation.get("component_receipts") or {}).get("development_executor")
            if not isinstance(integrated, dict) or integrated.get("main_sha") != sha:
                continue
            if not isinstance(released_receipt, dict) or not isinstance(receipt, dict):
                continue
            validate_release_receipt(receipt, "development_executor", sha, directory=directory)
            return [{"task_id": task.get("id"), "observation": receipt, "replayed": True}]
    return None


def observe(status: dict, *, state_dir: Path | None = None, runtime_root: Path | None = None,
            canonical_repo: Path | None = None, installed_bootstrap: Path | None = None) -> list[dict]:
    if not isinstance(status, dict) or status.get("result") != "complete" or status.get("exit_code") != 0:
        raise HiveError("runtime command did not succeed")
    if status.get("command") not in {"tick", "worker-tick", "team-work", "hive-merge"}:
        raise HiveError("unknown runtime command")
    try:
        start, finish = (datetime.fromisoformat(status[key]) for key in ("started_at", "finished_at"))
        if start.tzinfo is None or finish.tzinfo is None or finish < start:
            raise ValueError("invalid interval")
        repo = Path(status["release"])
        sha = status["runtime_sha"]
        bootstrap = hashlib.sha256((repo / "hive" / "runtime_entry.py").read_bytes()).hexdigest()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise HiveError("runtime receipt lacks verifiable execution observations") from exc
    if not runtime_root:
        runtime_root = os.environ.get("HIVE_RUNTIME_ROOT")
    if not canonical_repo:
        canonical_repo = os.environ.get("HIVE_RUNTIME_REPO")
    if not installed_bootstrap:
        installed_bootstrap = os.environ.get("HIVE_RUNTIME_BOOTSTRAP")
    if not runtime_root or not canonical_repo or not installed_bootstrap:
        raise HiveError("runtime receipt requires HIVE_RUNTIME_ROOT, HIVE_RUNTIME_REPO, and HIVE_RUNTIME_BOOTSTRAP")
    runtime_root = Path(runtime_root).resolve()
    canonical_repo = Path(canonical_repo).resolve()
    installed_bootstrap = Path(installed_bootstrap)
    if repo.is_symlink() or repo.resolve() != runtime_root / "releases" / sha:
        raise HiveError("runtime observation is not the configured installed release")
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common != _git(canonical_repo, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        raise HiveError("runtime release belongs to a different canonical Git hub")
    try:
        installed_digest = hashlib.sha256(installed_bootstrap.read_bytes()).hexdigest()
        persisted_bytes = (runtime_root / ("status-" + status["command"] + ".json")).read_bytes()
        persisted = json.loads(persisted_bytes)
    except (OSError, ValueError) as exc:
        raise HiveError("installed bootstrap or runtime status observation is missing") from exc
    execution_fields = ("command", "runtime_sha", "release", "result", "exit_code", "started_at", "finished_at", "bootstrap_sha256", "pid")
    if any(key not in status or persisted.get(key) != status[key] for key in execution_fields):
        raise HiveError("runtime input differs from the installed command status")
    if installed_digest != bootstrap:
        raise HiveError("installed bootstrap differs from runtime source")
    installation = {"runtime_root": str(runtime_root), "canonical_repo": str(canonical_repo), "common": common,
        "installed_bootstrap": str(installed_bootstrap.resolve()), "installed_bootstrap_sha256": installed_digest,
        "status_sha256": hashlib.sha256(persisted_bytes).hexdigest(), "status_hex": persisted_bytes.hex()}
    if status.get("bootstrap_sha256") != bootstrap:
        raise HiveError("installed runtime bootstrap differs from the admitted source")
    if _git(repo, "status", "--porcelain", "--untracked-files=normal"):
        raise HiveError("runtime source checkout is not clean")
    if resolve_ref(repo, "HEAD") != sha:
        raise HiveError("runtime observed HEAD differs from requested SHA")
    directory = state_directory(state_dir)
    try:
        gated = guard_ref(repo, sha, phase="release", state_dir=state_dir)
    except HiveError:
        replay = _released_generation_replay(sha, directory)
        if replay is not None:
            return replay
        raise
    results = []
    for task_id in gated["task_ids"]:
        delivery = load_task(task_id, state_dir=directory).get("delivery") or {}
        if "development_executor" not in (delivery.get("required_components") or []):
            continue
        previous = (delivery.get("component_receipts") or {}).get("development_executor")
        if previous:
            validate_release_receipt(previous, "development_executor", sha, directory=directory)
            results.append({"task_id": task_id, "observation": previous, "replayed": True})
            continue
        artifact = write_artifact(directory, "runtime", {"schema_version": 1, "observer": "runtime",
            "observed_sha": sha, "source_bootstrap_sha256": bootstrap, "observation": status, "installation": installation})
        receipt = released(task_id, repo, sha, sha, artifact=artifact,
                           component="development_executor", state_dir=directory)
        results.append({"task_id": task_id, "release": receipt})
    return results


def main() -> int:
    try:
        results = observe(json.load(sys.stdin))
    except (HiveError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "observations": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
