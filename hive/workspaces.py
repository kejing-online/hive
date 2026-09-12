"""Isolated Git workspaces and immutable dirty-tree checkpoints for Hive."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .state import HiveError


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "diff.external=", "-C", str(repo), *args], text=False, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HiveError("git command failed") from exc
    if result.returncode:
        raise HiveError(result.stderr.decode(errors="replace").strip() or "git command failed")
    return result.stdout.decode("utf-8", "surrogateescape")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise HiveError("workspace_id must be a 64-character lowercase SHA-256")
    return value


def _inside(path: Path, root: Path) -> bool:
    try: path.relative_to(root)
    except ValueError: return False
    return True


def _safe_path(value: Path) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise HiveError("persistent paths must not contain parent traversal")
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise HiveError("persistent paths must not traverse symlinks")
    return path


def _common(repo: Path) -> str:
    if not repo.is_dir() or _git(repo, "rev-parse", "--show-toplevel").strip() != str(repo):
        raise HiveError("repo must be a Git worktree root")
    return str(_safe_path(Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())))


def _atomic(path: Path, value: dict) -> None:
    path = _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def prepare(repo: Path, base_ref: str, *, workspace_id: str, root: Path) -> dict:
    """Create or replay a detached worktree; never reset an existing one."""
    identifier = _workspace_id(workspace_id)
    repo, root = _safe_path(repo), _safe_path(root)
    common = _common(repo)
    if _inside(root, repo) or _inside(repo, root):
        raise HiveError("workspace root must be outside repo")
    if not isinstance(base_ref, str) or not base_ref or base_ref.startswith("-"):
        raise HiveError("invalid base ref")
    base_sha = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}").strip()
    worktree = _safe_path(root / identifier)
    metadata = _safe_path(root / "metadata" / f"{identifier}.json")
    lock_path = _safe_path(root / ".lock")
    root.mkdir(parents=True, exist_ok=True)
    value = {"repo": str(repo), "base_sha": base_sha, "common": common,
             "worktree": str(worktree), "workspace_id": identifier}
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if metadata.exists():
            _manifest(value)
            return value
        if worktree.exists():
            raise HiveError("workspace path already exists without metadata")
        _git(repo, "worktree", "add", "--detach", str(worktree), base_sha)
        _atomic(metadata, value)
        return value


def _allowed(name: str, paths: list[str]) -> bool:
    return any(name == raw or name.startswith(raw + "/") for raw in paths)


def _validate_paths(paths: list[str]) -> None:
    if not isinstance(paths, list):
        raise HiveError("write_paths must be a list")
    for raw in paths:
        if (not isinstance(raw, str) or not raw or "\\" in raw or "\0" in raw
                or any(c in raw for c in "*?[]{}")
                or any(part in {"", ".", ".."} for part in raw.split("/"))):
            raise HiveError("invalid write path")


def _manifest(workspace: dict) -> None:
    required = {"repo", "base_sha", "common", "worktree", "workspace_id"}
    if not isinstance(workspace, dict) or set(workspace) != required:
        raise HiveError("invalid workspace manifest")
    identifier = _workspace_id(workspace["workspace_id"])
    if not isinstance(workspace["base_sha"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", workspace["base_sha"]):
        raise HiveError("invalid workspace base")
    for key in ("repo", "common", "worktree"):
        if not isinstance(workspace[key], str) or str(_safe_path(Path(workspace[key]))) != workspace[key]:
            raise HiveError("workspace paths must be absolute")
    tree, repo = Path(workspace["worktree"]), Path(workspace["repo"])
    if tree.name != identifier or _inside(tree, repo) or _inside(repo, tree.parent):
        raise HiveError("workspace location differs from identity")
    meta = _safe_path(tree.parent / "metadata" / f"{identifier}.json")
    _safe_path(tree.parent / ".lock")
    try:
        stored = json.loads(meta.read_text())
    except (OSError, ValueError) as exc:
        raise HiveError("workspace metadata is unreadable") from exc
    if stored != workspace:
        raise HiveError("workspace manifest differs")
    if _common(repo) != workspace["common"] or _common(tree) != workspace["common"]:
        raise HiveError("workspace common repository differs")


def _entries(worktree: Path, args: tuple[str, ...], entries: dict[str,str]) -> None:
    raw=_git(worktree,*args).split("\0"); i=0
    while i < len(raw) and raw[i]:
        status=raw[i]; i+=1
        count=2 if status[:1] in {"R","C"} else 1
        for name in raw[i:i+count]: entries[name]=status
        i+=count

def inspect(workspace: dict, write_paths: list[str]) -> dict:
    _validate_paths(write_paths)
    _manifest(workspace)
    worktree = Path(workspace["worktree"]); base = workspace["base_sha"]
    if worktree.is_symlink() or not worktree.is_dir(): raise HiveError("workspace path is invalid")
    entries: dict[str, str] = {}
    _entries(worktree,("diff","--name-status","-z",base),entries)
    _entries(worktree,("diff","--cached","--name-status","-z"),entries)
    _entries(worktree,("diff","--name-status","-z"),entries)
    for name in _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if name: entries[name] = "??"
    index = {}
    for row in _git(worktree, "ls-files", "--stage", "-z").split("\0"):
        if row:
            stage, name = row.split("\t", 1)
            index.setdefault(name, []).append(stage)
    files=[]; violations=[]
    for name, status in sorted(entries.items()):
        path = worktree / name
        item={"path": name, "status": status, "sha256": None, "mode": None, "index": []}
        item["index"] = index.get(name, [])
        if any(stage.startswith("120000 ") for stage in item["index"]):
            violations.append(f"symlink in index: {name}")
        if not _allowed(name, write_paths): violations.append(f"out of scope: {name}")
        try:
            parent=path.parent
            escaped=False
            while parent != worktree:
                if parent.is_symlink(): violations.append(f"symlink ancestor: {name}"); escaped=True; break
                parent=parent.parent
            if not escaped:
                if path.is_symlink():
                    violations.append(f"symlink changed: {name}")
                elif path.exists():
                    mode = path.stat().st_mode
                    item["mode"] = stat.S_IMODE(mode)
                    if stat.S_ISREG(mode):
                        item["sha256"] = _sha(path)
                    else:
                        violations.append(f"unsupported changed file type: {name}")
        except OSError: violations.append(f"unreadable changed path: {name}")
        files.append(item)
    head=_git(worktree,"rev-parse","HEAD").strip()
    if head != base: violations.append("HEAD differs from base")
    return {"base_sha": base, "head_sha": head, "files": files, "violations": violations, "clean": not files and head == base}


def checkpoint(workspace: dict, write_paths: list[str], *, remaining: list[str], output: Path) -> dict:
    if not isinstance(remaining, list) or any(not isinstance(item, str) for item in remaining): raise HiveError("remaining must be strings")
    output = _safe_path(output)
    if _inside(output, Path(workspace["worktree"])) or _inside(output, Path(workspace["repo"])):
        raise HiveError("checkpoint output must be outside repository and workspace")
    result={"workspace": workspace, "write_paths": write_paths, "remaining": remaining, "at": datetime.now(timezone.utc).isoformat(), "inspection": inspect(workspace, write_paths)}
    _atomic(output, result); return result


def assert_checkpoint(workspace: dict, checkpoint_path: Path) -> dict:
    checkpoint_path = _safe_path(checkpoint_path)
    try: saved=json.loads(checkpoint_path.read_text())
    except (OSError, ValueError) as exc: raise HiveError("checkpoint is unreadable") from exc
    if not isinstance(saved, dict) or saved.get("workspace") != workspace or not isinstance(saved.get("inspection"), dict): raise HiveError("checkpoint identity does not match workspace")
    if saved["inspection"].get("violations"): raise HiveError("checkpoint has scope violations")
    current=inspect(workspace, saved.get("write_paths", []))
    # Scope already passed at checkpoint; exact scan detects additions/removals/content and HEAD changes.
    if current["violations"] or current["base_sha"] != saved["inspection"].get("base_sha") or current["head_sha"] != saved["inspection"].get("head_sha") or current["files"] != saved["inspection"].get("files"):
        raise HiveError("workspace differs from checkpoint")
    return saved
