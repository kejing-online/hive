"""Git-bound Hive integration and deployment receipts (no deployment actions)."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guard import validate
from .state import HiveError, _mark, _read, _save, locked_task, state_directory

SHA = re.compile(r"^[0-9a-f]{40}$")
COMPONENTS = {"backend", "frontend", "development_executor", "repository"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                                timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HiveError("git command failed") from exc
    if result.returncode:
        raise HiveError("git " + args[0] + " failed")
    return result.stdout.strip()


def resolve_ref(repo_path: Path, ref: str) -> str:
    repo = Path(repo_path)
    if not isinstance(ref, str) or not ref.strip():
        raise HiveError("git ref is required")
    sha = _git(repo, "rev-parse", "--verify", "--end-of-options", ref.strip() + "^{commit}")
    if not SHA.fullmatch(sha):
        raise HiveError("git ref did not resolve to a full commit SHA")
    return sha


def _repo_name(repo: Path) -> str:
    try:
        raw = _git(repo, "remote", "get-url", "origin").strip().rstrip("/")
    except HiveError:
        return "local/" + Path(repo).name
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", raw)
    if match:
        return match.group(1)
    return "local/" + Path(repo).name


def _evidence_fingerprint(task: dict[str, Any]) -> str:
    nodes = []
    for node in task["nodes"]:
        if node["id"] in {"integrate", "release"}:
            continue
        nodes.append({key: node.get(key) for key in ("id", "depends_on", "status", "owner", "artifacts", "approved_at")})
    if "factory_scope" in (task.get("context") or {}):
        nodes.append({"factory_scope": task["context"]["factory_scope"]})
    blob = json.dumps(nodes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _context_repo(task: dict[str, Any], repo: Path) -> str:
    if task.get("kind", "development") != "development":
        raise HiveError("analysis tasks cannot register delivery")
    expected = (task.get("context") or {}).get("repo")
    if not isinstance(expected, str) or not expected.strip():
        raise HiveError("Hive task has no bound repository context")
    actual = _repo_name(repo)
    if actual != expected.strip():
        raise HiveError("git origin does not match Hive repository context")
    return actual


def _delivery(task: dict[str, Any]) -> dict[str, Any]:
    value = task.get("delivery")
    if not isinstance(value, dict):
        raise HiveError("Hive task has no registered delivery ref")
    for key in ("repo", "code_sha", "evidence_fingerprint"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise HiveError("invalid registered delivery receipt")
    return value


def _registered_current(task: dict[str, Any], repo: Path, sha: str | None = None, directory: Path | None = None) -> dict[str, Any]:
    delivery = _delivery(task)
    if delivery["repo"] != _context_repo(task, repo):
        raise HiveError("registered delivery repository mismatch")
    if sha is not None and delivery["code_sha"] != sha:
        raise HiveError("Hive task is not registered for this code SHA")
    if delivery["evidence_fingerprint"] != _evidence_fingerprint(task):
        raise HiveError("registered Hive evidence fingerprint is stale")
    if delivery.get("schema_version") != 1 or not delivery.get("base_sha"):
        raise HiveError("legacy delivery requires fresh source-bound registration")
    trusted_base(repo, delivery["code_sha"], delivery["base_sha"])
    verify_incoming_scope(repo, delivery, delivery["base_sha"])
    from .evidence import validate_source_evidence
    validate_source_evidence(task, repo, delivery["code_sha"], delivery.get("files") or {}, state_directory(directory))
    return delivery


def trusted_base(repo: Path, sha: str, base_ref: str = "") -> str:
    """Only a main ancestor can be an explicit comparison baseline."""
    try:
        main = resolve_ref(repo, "refs/remotes/origin/main")
    except HiveError as exc:
        raise HiveError("trusted origin/main baseline is missing; fetch main before capture/register") from exc
    if base_ref:
        base = resolve_ref(repo, base_ref)
        try:
            _git(repo, "merge-base", "--is-ancestor", base, main)
            _git(repo, "merge-base", "--is-ancestor", base, sha)
        except HiveError as exc:
            raise HiveError("explicit base must be a trusted main ancestor of source") from exc
        return base
    return _git(repo, "merge-base", sha, main)


def changed_files(repo: Path, sha: str, base_ref: str = "") -> dict[str, str]:
    """Exact reviewed file objects from a trusted main baseline."""
    base = trusted_base(repo, sha, base_ref)
    names = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, sha).split("\0")
    return {name: _git(repo, "ls-tree", sha, "--", name) for name in names if name}


def verify_incoming_scope(repo: Path, delivery: dict, main_sha: str) -> None:
    """Independently check every change being brought onto the fetched main."""
    base = _git(repo, "merge-base", delivery["code_sha"], main_sha)
    names = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, delivery["code_sha"]).split("\0")
    registered = delivery.get("files") or {}
    for name in filter(None, names):
        if name not in registered or registered[name] != _git(repo, "ls-tree", delivery["code_sha"], "--", name):
            raise HiveError("unregistered incoming source scope: " + name)


def components_for(files: dict[str, str]) -> list[str]:
    required = set()
    for name in files:
        if name.startswith(("src/", "backend/", "server/")):
            required.add("backend")
        elif name.startswith(("web/", "frontend/", "ui/")):
            required.add("frontend")
        elif name.startswith(("hive/", "scripts/", "docs/")):
            required.add("development_executor")
    return sorted(required or {"repository"})


def verify_merged_files(repo: Path, code_sha: str, main_sha: str, files: dict[str, str]) -> None:
    _git(repo, "merge-base", "--is-ancestor", code_sha, main_sha)
    for name, expected in files.items():
        if _git(repo, "ls-tree", main_sha, "--", name) != expected:
            raise HiveError("merged content differs from verified source: " + name + "; update branch and reverify affected scope")


def register(task_id: str, repo_path: Path, ref: str, *, state_dir: Path | None = None,
             rework_reason: str = "", base_ref: str = "") -> dict[str, Any]:
    repo = Path(repo_path)
    sha = resolve_ref(repo, ref)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        repo_name = _context_repo(task, repo)
        validate(task, phase="integrate")
        files = changed_files(repo, sha, base_ref)
        previous = task.get("delivery")
        if previous and previous.get("code_sha") == sha:
            files = previous.get("files") or files
        from .evidence import validate_source_evidence
        source_evidence = validate_source_evidence(task, repo, sha, files, directory)
        delivery = {"repo": repo_name, "code_sha": sha, "evidence_fingerprint": _evidence_fingerprint(task),
                    "registered_at": _now()}
        previous = task.get("delivery")
        if previous:
            same = all(previous.get(key) == delivery[key] for key in ("repo", "code_sha", "evidence_fingerprint"))
            if same:
                return dict(previous)
            if not isinstance(rework_reason, str) or not rework_reason.strip():
                raise HiveError("delivery ref already registered; explicit rework registration required")
            # New passing evidence is required above. Re-registration explicitly
            # revokes old merge/release receipts rather than recycling approval.
            # Keep the last factual release immutable while the replacement
            # revision is being re-integrated.  Runtime bootstrap can use this
            # generation as a safe fallback; the new delivery itself remains
            # queued and cannot inherit the old approval.
            if previous.get("integrated") or previous.get("released"):
                generations = task.setdefault("delivery_generations", [])
                if not any(g.get("code_sha") == previous.get("code_sha") for g in generations):
                    generations.append(dict(previous))
            _mark(task, "integrate", "queued", owner="hive:integrator", artifact=rework_reason,
                  approved=False, retry=True, delivery_write=True)
            task["history"].append({"event": "delivery_rework", "at": _now(),
                                    "reason": rework_reason.strip(), "previous": previous})
        if not files:
            raise HiveError("no unintegrated source change to register; existing receipts may only be replayed")
        scope = (task.get("context") or {}).get("factory_scope") or {}
        if scope.get("allowed_paths") is not None:
            import fnmatch
            for name in files:
                if not any(fnmatch.fnmatch(name, pattern) for pattern in scope["allowed_paths"]):
                    raise HiveError("registered file is outside factory-authorized scope: " + name)
        delivery.update(files=files, required_components=components_for(files),
                        base_sha=trusted_base(repo, sha, base_ref), source_evidence=source_evidence, schema_version=1)
        if previous != delivery:
            task["delivery"] = delivery
            task["history"].append({"event": "delivery_registered", "at": _now(), "code_sha": sha})
            _save(task, directory)
        return dict(task["delivery"])


def _find_registered(repo: Path, sha: str, directory: Path) -> list[str]:
    found: list[str] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("HIVE-*.json")):
        if path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        delivery = value.get("delivery") if isinstance(value, dict) else None
        if isinstance(delivery, dict) and delivery.get("code_sha") == sha:
            found.append(str(value.get("id") or ""))
    return [task_id for task_id in found if task_id]


def _find_integrated(sha: str, directory: Path) -> list[str]:
    """Read the bounded Hive index before taking each matching task lock."""
    found: list[str] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("HIVE-*.json")):
        if path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        delivery = value.get("delivery") if isinstance(value, dict) else None
        integrated = delivery.get("integrated") if isinstance(delivery, dict) else None
        if isinstance(integrated, dict) and integrated.get("main_sha") == sha:
            found.append(str(value.get("id") or ""))
    return [task_id for task_id in found if task_id]


def guard_ref(repo_path: Path, ref: str, *, phase: str = "integrate",
              state_dir: Path | None = None) -> dict[str, Any]:
    if phase not in {"integrate", "release"}:
        raise HiveError("phase must be integrate or release")
    repo = Path(repo_path)
    sha = resolve_ref(repo, ref)
    directory = state_directory(state_dir)
    matches = _find_registered(repo, sha, directory) if phase == "integrate" else _find_integrated(sha, directory)
    if not matches:
        raise HiveError("no Hive task is registered for this code SHA")
    ready: list[str] = []
    for task_id in matches:
        with locked_task(task_id, directory) as locked:
            task = _read(task_id, locked)
            delivery = _registered_current(task, repo, sha if phase == "integrate" else None, directory=locked)
            if phase == "release":
                integrated = delivery.get("integrated")
                if not isinstance(integrated, dict) or integrated.get("main_sha") != sha:
                    raise HiveError("registered integration receipt changed during guard")
                if resolve_ref(repo, "HEAD") != sha:
                    raise HiveError("integrated main SHA is not the current repository HEAD")
            validate(task, phase=phase)
            ready.append(task_id)
    if not ready:
        raise HiveError("no registered Hive task passed the " + phase + " gate")
    return {"ref": sha, "phase": phase, "task_ids": ready}


def integrated(task_id: str, repo_path: Path, main_ref: str, *, artifact: str,
               state_dir: Path | None = None) -> dict[str, Any]:
    if not isinstance(artifact, str) or not artifact.strip():
        raise HiveError("integration receipt requires an artifact")
    repo = Path(repo_path)
    main_sha = resolve_ref(repo, main_ref)
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        delivery = _registered_current(task, repo, directory=directory)
        code_sha = delivery["code_sha"]
        # `_git` treats nonzero as a HiveError, so success proves ancestry.
        _git(repo, "merge-base", "--is-ancestor", code_sha, main_sha)
        _git(repo, "merge-base", "--is-ancestor", main_sha, "refs/remotes/origin/main")
        if "files" not in delivery:
            raise HiveError("legacy delivery requires explicit source-scope adoption")
        verify_merged_files(repo, code_sha, main_sha, delivery["files"])
        validate(task, phase="integrate")
        previous = delivery.get("integrated")
        if previous:
            if previous.get("main_sha") != main_sha or previous.get("artifact") != artifact.strip():
                raise HiveError("integration receipt already recorded with different observations")
            return dict(previous)
        delivery["integrated"] = {"main_sha": main_sha, "artifact": artifact.strip(), "at": _now()}
        _mark(task, "integrate", "succeeded", owner="hive:integrator", artifact=artifact.strip(), approved=True, delivery_write=True)
        delivery["release_pending"] = True
        task["history"].append({"event": "delivery_integrated", "at": _now(), "main_sha": main_sha})
        _save(task, directory)
        return dict(delivery["integrated"])


def admit_landed_main(repo_path: Path, ref: str = "refs/remotes/origin/main", *,
                      artifact: str = "", state_dir: Path | None = None) -> dict[str, Any]:
    """Register a GitHub-landed origin/main tip without hive.cli node integrate/release."""
    repo = Path(repo_path)
    main_sha = resolve_ref(repo, "refs/remotes/origin/main")
    sha = resolve_ref(repo, ref)
    if sha != main_sha:
        raise HiveError("landed admission is only for the current origin/main tip")
    directory = state_directory(state_dir)
    already = _find_integrated(sha, directory)
    if already:
        return {"already": True, "main_sha": sha, "task_ids": already, "required_components": []}
    try:
        parent = _git(repo, "rev-parse", "--verify", sha + "^1^{commit}")
    except HiveError as exc:
        raise HiveError("landed admission requires origin/main to have a parent commit") from exc
    from .evidence import capture_source, record_evidence, write_artifact
    from .intake import ensure_task
    from .state import mark_node
    task = ensure_task(
        "GitHub-landed origin/main " + sha[:12],
        repo=_repo_name(repo),
        source_key="main-cd:landed:" + sha,
        size="S",
        kind="development",
        state_dir=directory,
    )
    task_id = task["id"]
    proof = write_artifact(directory, "source", {
        "schema_version": 1, "kind": "github-landed-main", "task_id": task_id,
        "main_sha": sha, "parent_sha": parent,
    })
    mark_node(task_id, "intake", "succeeded", owner="hive:landed-intake", artifact=proof, state_dir=directory)
    mark_node(task_id, "plan", "succeeded", owner="hive:landed-plan", artifact=proof, state_dir=directory)
    mark_node(task_id, "implement", "succeeded", owner="hive:landed-implement", artifact=proof, state_dir=directory)
    snapshot = capture_source(task_id, repo, sha, state_dir=directory, base_ref=parent)
    with tempfile.TemporaryDirectory(prefix="hive-landed-report-") as name:
        verify_report = Path(name) / "verify.txt"
        review_report = Path(name) / "review.txt"
        verify_report.write_text("landed origin/main first-parent scope verified\n")
        review_report.write_text("landed origin/main first-parent scope reviewed\n")
        time.sleep(0.05)
        verify_report.touch()
        review_report.touch()
        record_evidence(task_id, "verify", snapshot, verify_report,
                        owner="hive:landed-verify", state_dir=directory)
        record_evidence(task_id, "review", snapshot, review_report,
                        owner="hive:landed-review", state_dir=directory)
    delivery = register(task_id, repo, sha, state_dir=directory, base_ref=parent)
    receipt = integrated(task_id, repo, sha, artifact=(artifact.strip() or ("github-landed:" + sha)),
                         state_dir=directory)
    return {"already": False, "main_sha": sha, "task_id": task_id, "task_ids": [task_id],
            "required_components": delivery.get("required_components") or [], "integrated": receipt}


def released(task_id: str, repo_path: Path, expected_ref: str, observed_ref: str, *, artifact: str,
             component: str | list[str], state_dir: Path | None = None) -> dict[str, Any]:
    if not isinstance(artifact, str) or not artifact.strip():
        raise HiveError("release receipt requires an artifact")
    components = [component] if isinstance(component, str) else component
    if not isinstance(components, list) or not components or any(not isinstance(x, str) or not x.strip() for x in components):
        raise HiveError("release receipt requires nonempty component fields")
    if set(components) - COMPONENTS:
        raise HiveError("unknown release component")
    repo = Path(repo_path)
    expected = resolve_ref(repo, expected_ref)
    observed = resolve_ref(repo, observed_ref)
    if expected != observed:
        raise HiveError("release expected SHA does not match observed SHA")
    with locked_task(task_id, state_dir) as directory:
        task = _read(task_id, directory)
        delivery = _registered_current(task, repo, directory=directory)
        integrated_receipt = delivery.get("integrated")
        if not isinstance(integrated_receipt, dict) or integrated_receipt.get("main_sha") != expected:
            raise HiveError("release SHA is not the recorded integrated main SHA")
        if resolve_ref(repo, "HEAD") != expected:
            raise HiveError("integrated main SHA is not the current repository HEAD")
        validate(task, phase="release")
        required = set(delivery.get("required_components") or [])
        if not required or required - COMPONENTS:
            raise HiveError("delivery has no valid required component scope")
        if set(components) - required:
            raise HiveError("receipt component is outside this task's required scope")
        from .receipt_validation import validate_release_receipt
        from .evidence import read_artifact
        incoming = read_artifact(artifact.strip())
        observations = delivery.setdefault("component_receipts", {})
        for name in components:
            previous = observations.get(name)
            if previous and (previous.get("observed_sha") != observed or previous.get("artifact") != artifact.strip()):
                raise HiveError("release receipt already recorded with different observations")
            candidate = {"observed_sha": observed, "artifact": artifact.strip(), "at": _now(),
                         "schema_version": 1, "observer": incoming.get("observer"), "component": name}
            validate_release_receipt(candidate, name, expected, directory=directory)
            if previous:
                validate_release_receipt(previous, name, expected, directory=directory)
            observations[name] = previous or candidate
        for name, observation in observations.items():
            validate_release_receipt(observation, name, expected, directory=directory)
        remaining = sorted(required - observations.keys())
        receipt = {"expected_sha": expected, "observed_sha": observed, "artifact": artifact.strip(),
                   "components": sorted(observations), "remaining_components": remaining,
                   "complete": not remaining, "at": _now()}
        if not remaining:
            if delivery.get("released"):
                return dict(delivery["released"])
            delivery["released"] = receipt
            _mark(task, "release", "succeeded", owner="hive:deployer", artifact=artifact.strip(), approved=True, delivery_write=True)
            delivery["release_pending"] = False
        task["history"].append({"event": "delivery_released" if not remaining else "delivery_component_observed",
                                "at": _now(), "observed_sha": observed, "components": list(components)})
        _save(task, directory)
        return dict(receipt)
