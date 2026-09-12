"""The sole Hive-controlled Git integration entrypoint (no deployment actions)."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .delivery import guard_ref, integrated, released, resolve_ref, verify_merged_files, verify_incoming_scope
from .state import HiveError, load_task, state_directory

PARKED_PREFIXES = ("dependabot/", "release/", "wip/", "claude/", "cursor/", "tmp/", "execute-plan/")
PROTECTED = {"main", "master", "prod", "live"}


class IntegratorError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], text=True, capture_output=True, timeout=60)
    if check and result.returncode:
        raise IntegratorError((result.stderr or result.stdout or "git failed").strip())
    return result


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegratorError("another Hive merge is already running") from exc
        yield


def eligible_branch(name: str) -> bool:
    return bool(name) and name not in PROTECTED and not name.startswith(PARKED_PREFIXES)


def _branches(repo: Path, selected: set[str]) -> list[tuple[str, str]]:
    output = _git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/remotes/origin").stdout
    values: list[tuple[str, str]] = []
    for line in output.splitlines():
        short, _, sha = line.partition(" ")
        if not short.startswith("origin/") or short == "origin/HEAD":
            continue
        name = short.removeprefix("origin/")
        if eligible_branch(name) and (not selected or name in selected):
            values.append((name, sha))
    return sorted(values)


def _task_ids(repo: Path, sha: str, directory: Path) -> list[str]:
    try:
        return list(guard_ref(repo, sha, phase="integrate", state_dir=directory)["task_ids"])
    except HiveError:
        return []


def _scope_matches(repo: Path, task_id: str, main_sha: str, directory: Path) -> dict[str, Any]:
    task = load_task(task_id, state_dir=directory)
    delivery = task.get("delivery")
    if not isinstance(delivery, dict) or not isinstance(delivery.get("files"), dict):
        raise IntegratorError("Hive task has no registered immutable file scope: " + task_id)
    try:
        verify_incoming_scope(repo, delivery, delivery.get("base_sha") or "refs/remotes/origin/main")
        verify_merged_files(repo, delivery["code_sha"], main_sha, delivery["files"])
    except HiveError as exc:
        raise IntegratorError("Hive source scope changed for " + task_id + ": " + str(exc)) from exc
    return task


def _release_repository_only(task_id: str, repo: Path, main_sha: str, directory: Path, artifact: str) -> dict[str, Any] | None:
    task = load_task(task_id, state_dir=directory)
    delivery = task.get("delivery") if isinstance(task, dict) else None
    if not isinstance(delivery, dict) or delivery.get("required_components") != ["repository"]:
        return None
    from .repository_receipts import observe
    return observe(task_id, repo, main_sha, state_dir=directory)


def _pending_release(repo: Path, directory: Path, main_sha: str) -> bool:
    """Return whether current main has an integrated delivery awaiting receipt.

    This scan is deliberately durable: a later merge run may have no new
    branch candidate, but must still keep requesting deployment until every
    required component receipt completes.
    """
    if not directory.is_dir():
        return False
    for path in directory.glob("HIVE-*.json"):
        if path.is_symlink():
            continue
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        delivery = task.get("delivery") if isinstance(task, dict) else None
        integrated_receipt = delivery.get("integrated") if isinstance(delivery, dict) else None
        if not isinstance(integrated_receipt, dict) or integrated_receipt.get("main_sha") != main_sha:
            continue
        if delivery.get("released") is None or delivery.get("release_pending") is True:
            return True
    return False


def run(repo_path: Path, *, branches: list[str] | None = None, state_dir: Path | None = None,
        dry_run: bool = False, auto_deploy: bool = False,
        lock_path: Path = Path(__import__("os").environ.get("HIVE_INTEGRATOR_LOCK") or "/tmp/hive-integrator.lock")) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise IntegratorError("repository must be a Git checkout")
    directory = state_directory(state_dir)
    selected = set(branches or [])
    if any(not eligible_branch(name) for name in selected):
        raise IntegratorError("requested branch is protected or parked")
    outcome: dict[str, Any] = {"dry_run": dry_run, "auto_deploy_requested": auto_deploy,
                               "merged": [], "already_integrated": [], "skipped": [], "needs_release": False}
    with _lock(lock_path):
        _git(repo, "fetch", "origin", "--prune")
        candidates = [(name, sha, _task_ids(repo, sha, directory)) for name, sha in _branches(repo, selected)]
        candidates = [(name, sha, ids) for name, sha, ids in candidates if ids]
        if not candidates:
            current_main = resolve_ref(repo, "refs/remotes/origin/main")
            outcome["main_sha"] = current_main
            outcome["needs_release"] = _pending_release(repo, directory, current_main)
            outcome["reason"] = "no Hive-guarded branch candidates"; return outcome
        with tempfile.TemporaryDirectory(prefix="hive-integrator-") as temp:
            worktree = Path(temp) / "main"; _git(repo, "worktree", "add", "--detach", str(worktree), "origin/main")
            try:
                main_sha = resolve_ref(worktree, "HEAD"); pending: list[tuple[str, str, list[str]]] = []
                for name, sha, task_ids in candidates:
                    if _git(worktree, "merge-base", "--is-ancestor", sha, main_sha, check=False).returncode == 0:
                        missing: list[str] = []
                        for task_id in task_ids:
                            task = load_task(task_id, state_dir=directory)
                            # Completed/integrated history is not a candidate and
                            # must not be revalidated against unrelated newer main.
                            if (task.get("delivery") or {}).get("integrated"):
                                continue
                            _scope_matches(worktree, task_id, main_sha, directory)
                            missing.append(task_id)
                        if missing:
                            outcome["already_integrated"].append(name); pending.append((name, sha, missing))
                        else:
                            outcome["skipped"].append({"branch": name, "reason": "already has an integration receipt"})
                        continue
                    if outcome["merged"]:
                        outcome["skipped"].append({"branch": name, "reason": "one new branch per Hive merge run"}); continue
                    try:
                        for task_id in task_ids:
                            verify_incoming_scope(worktree, load_task(task_id, state_dir=directory)["delivery"], main_sha)
                    except HiveError as exc:
                        outcome["skipped"].append({"branch": name, "reason": str(exc)})
                        continue
                    merged = _git(worktree, "merge", "--no-ff", "--no-commit", sha, check=False)
                    if merged.returncode:
                        _git(worktree, "merge", "--abort", check=False)
                        outcome["skipped"].append({"branch": name, "reason": "merge conflict or Git refusal"}); continue
                    _git(worktree, "commit", "--no-verify", "--no-edit")
                    proposed = resolve_ref(worktree, "HEAD")
                    try:
                        for task_id in task_ids: _scope_matches(worktree, task_id, proposed, directory)
                    except IntegratorError:
                        _git(worktree, "reset", "--hard", "HEAD~1")
                        outcome["skipped"].append({"branch": name, "reason": "registered source scope differs after merge"}); continue
                    main_sha = proposed; outcome["merged"].append(name); pending.append((name, sha, task_ids))
                if not pending:
                    outcome["main_sha"] = main_sha
                    outcome["needs_release"] = _pending_release(worktree, directory, main_sha)
                    return outcome
                if dry_run:
                    outcome["planned_main_sha"] = main_sha; return outcome
                # A later clean merge can alter an earlier reviewed file.  Re-run
                # both exact-SHA admission and immutable blob comparison for every
                # pending receipt before the one allowed remote write.
                for _name, source_sha, task_ids in pending:
                    current = _task_ids(worktree, source_sha, directory)
                    for task_id in task_ids:
                        if task_id not in current:
                            raise IntegratorError("Hive admission changed before main push: " + task_id)
                        _scope_matches(worktree, task_id, main_sha, directory)
                if outcome["merged"]:
                    _git(worktree, "push", "--no-verify", "origin", "HEAD:main")
                    _git(worktree, "fetch", "origin", "main")
                    remote_sha = resolve_ref(worktree, "refs/remotes/origin/main")
                    if remote_sha != main_sha: raise IntegratorError("origin/main differs after push; no Hive integration receipt written")
                else: remote_sha = main_sha
                artifact = "git://origin/main#sha=" + remote_sha
                for _name, _sha, task_ids in pending:
                    for task_id in task_ids:
                        _scope_matches(worktree, task_id, remote_sha, directory)
                        integrated(task_id, worktree, remote_sha, artifact=artifact, state_dir=directory)
                        _release_repository_only(task_id, worktree, remote_sha, directory, artifact)
                outcome["main_sha"] = remote_sha
                outcome["needs_release"] = _pending_release(worktree, directory, remote_sha)
                return outcome
            finally:
                _git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("HIVE_CANONICAL_REPO") or Path.cwd()))
    parser.add_argument("--branch", action="append", default=[])
    parser.add_argument("--state-dir", default=""); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-deploy", choices=("0", "1"), default="0")
    parser.add_argument("--lock-path", default="/tmp/hive-integrator.lock")
    args = parser.parse_args(argv)
    try:
        result = run(args.repo, branches=args.branch, state_dir=Path(args.state_dir) if args.state_dir else None,
                     dry_run=args.dry_run, auto_deploy=args.auto_deploy == "1", lock_path=Path(args.lock_path))
    except (IntegratorError, HiveError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)); return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())
