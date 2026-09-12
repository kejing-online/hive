"""Bind factual verification/review artifacts to a captured Git source tree.

Capture before running a check/review; record its existing report afterwards.
This is an auditable local actor attestation, not an OS-level signature service.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .state import HiveError, load_task, mark_node, state_directory

PHASES = {"verify", "review", "security"}


def write_artifact(directory: Path, category: str, value: dict) -> str:
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    root = directory / "receipts" / category
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + ".json")
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise HiveError("immutable artifact collision")
    return str(path.resolve()) + "#sha256=" + digest


def read_artifact(artifact: str, *, root: Path | None = None) -> dict:
    raw, sep, tail = str(artifact).partition("#sha256=")
    digest = tail.split(";", 1)[0]
    path = Path(raw)
    if (not sep or len(digest) != 64 or not path.is_absolute() or path.is_symlink()
            or (root is not None and path.resolve().parent != root.resolve())):
        raise HiveError("invalid immutable artifact reference")
    try:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise HiveError("artifact digest differs from recorded evidence")
        value = json.loads(data)
    except HiveError:
        raise
    except (OSError, ValueError) as exc:
        raise HiveError("artifact file is missing or invalid") from exc
    if not isinstance(value, dict):
        raise HiveError("artifact must contain a JSON object")
    return value


def capture_source(task_id: str, repo: Path, ref: str = "HEAD", *, worktree: bool = False,
                   state_dir: Path | None = None, paths: list[str] | None = None,
                   base_ref: str = "") -> str:
    from .delivery import _context_repo, _git, resolve_ref, trusted_base
    captured_at_ns = time.time_ns()
    directory = state_directory(state_dir)
    task = load_task(task_id, state_dir=directory)
    repo_name = _context_repo(task, repo)
    sha = resolve_ref(repo, ref)
    tree = _git(repo, "rev-parse", sha + "^{tree}")
    if worktree:
        if sha != resolve_ref(repo, "HEAD"):
            raise HiveError("worktree source capture requires HEAD")
        with tempfile.TemporaryDirectory(prefix="hive-evidence-index-") as name:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(name) / "index")}
            for args in (("read-tree", "HEAD"), ("add", "-A"), ("write-tree",)):
                result = subprocess.run(["git", "-C", str(repo), *args], env=env,
                                        capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise HiveError("cannot capture worktree source")
            tree = result.stdout.strip()
    base = trusted_base(repo, sha, base_ref)
    names = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, tree).split("\0")
    files = {name: _git(repo, "ls-tree", tree, "--", name) for name in names
             if name and (not paths or any(fnmatch.fnmatchcase(name, pattern) for pattern in paths))}
    return write_artifact(directory, "source", {"schema_version": 1, "task_id": task_id,
        "captured_at_ns": captured_at_ns, "repo": repo_name, "commit": sha, "tree": tree, "base_sha": base, "files": files, "scope_patterns": paths or ["*"]})


def record_evidence(task_id: str, phase: str, snapshot: str | list[str], report: Path, *, owner: str,
                    state_dir: Path | None = None) -> dict:
    if phase not in PHASES or not owner.strip():
        raise HiveError("source evidence requires a verification/review phase and actor")
    directory = state_directory(state_dir)
    snapshots = [snapshot] if isinstance(snapshot, str) else snapshot
    sources = [read_artifact(item, root=directory / "receipts" / "source") for item in snapshots]
    if not sources or any(source.get("task_id") != task_id for source in sources):
        raise HiveError("source snapshot belongs to another Hive task")
    try:
        report_data = report.read_bytes()
        report_mtime_ns = report.stat().st_mtime_ns
    except OSError as exc:
        raise HiveError("evidence requires an existing report file") from exc
    capture_times = [source.get("captured_at_ns") or Path(ref.split("#sha256=", 1)[0]).stat().st_mtime_ns
                     for source, ref in zip(sources, snapshots)]
    if any(report_mtime_ns < captured for captured in capture_times):
        raise HiveError("evidence report predates source capture; capture before verification/review")
    if not report_data.strip():
        raise HiveError("evidence report is empty")
    # Copy the report into the content-addressed receipt itself; its old mutable
    # pathname is descriptive only and cannot later change the approved content.
    artifact = write_artifact(directory, "evidence", {"schema_version": 1, "task_id": task_id,
        "phase": phase, "owner": owner.strip(), "sources": sources, "snapshots": snapshots,
        "capture_times_ns": capture_times,
        "report_path": str(report.resolve()), "report_mtime_ns": report_mtime_ns, "report_sha256": hashlib.sha256(report_data).hexdigest(),
        "report_hex": report_data.hex()})
    return mark_node(task_id, phase, "succeeded", owner=owner, artifact=artifact, state_dir=directory)


def validate_source_evidence(task: dict, repo: Path, sha: str, files: dict[str, str],
                             directory: Path) -> dict:
    from .delivery import _git
    result = {}
    for node in task["nodes"]:
        if node["id"] not in PHASES or not node.get("required", True):
            continue
        artifacts = node.get("artifacts") or []
        if not artifacts:
            raise HiveError("missing source-bound evidence: " + node["id"])
        receipt = read_artifact(artifacts[-1], root=directory / "receipts" / "evidence")
        if (receipt.get("schema_version") != 1 or receipt.get("task_id") != task["id"]
                or receipt.get("phase") != node["id"] or receipt.get("owner") != node.get("owner")):
            raise HiveError("source evidence actor/phase/task mismatch")
        sources = receipt.get("sources") or []
        snapshots = receipt.get("snapshots") or []
        if not sources or len(sources) != len(snapshots):
            raise HiveError("source evidence snapshot list is missing")
        captured = {}
        for index, (source, snapshot_ref) in enumerate(zip(sources, snapshots)):
            snapshot = read_artifact(snapshot_ref, root=directory / "receipts" / "source")
            if source != snapshot or source.get("repo") != task["context"]["repo"] or source.get("task_id") != task["id"]:
                raise HiveError("source evidence snapshot mismatch")
            captured_at = source.get("captured_at_ns")
            if not captured_at:
                # Compatibility only for immutable snapshots captured before the
                # timestamp field existed. Preserve their original file metadata.
                capture_times = receipt.get("capture_times_ns") or []
                captured_at = capture_times[index] if index < len(capture_times) else 0
            if not captured_at or receipt.get("report_mtime_ns", 0) < captured_at:
                raise HiveError("source evidence predates source capture")
            scoped = source.get("files")
            if not isinstance(scoped, dict):
                raise HiveError("source evidence has no captured files")
            for name, blob in scoped.items():
                captured.setdefault(name, set()).add(blob)
        try:
            report_data = bytes.fromhex(receipt["report_hex"])
        except (KeyError, ValueError, TypeError) as exc:
            raise HiveError("source evidence report is invalid") from exc
        if not report_data or hashlib.sha256(report_data).hexdigest() != receipt.get("report_sha256"):
            raise HiveError("source evidence report digest mismatch")
        # Worktree snapshot trees can be unreachable objects after commit/GC or
        # absent in another clone. The immutable captured blob map is portable;
        # compare it directly to the registered commit, never trust a later path.
        if any(blob not in captured.get(name, set()) or _git(repo, "ls-tree", sha, "--", name) != blob
               for name, blob in files.items()):
            raise HiveError("source-bound evidence does not cover changed source: " + node["id"])
        result[node["id"]] = {"artifact": artifacts[-1], "sources": [
            {"tree": source["tree"], "commit": source["commit"], "files": source["files"]} for source in sources],
            "reused_identical_blobs": any(source["tree"] != _git(repo, "rev-parse", sha + "^{tree}") for source in sources)}
    return result
