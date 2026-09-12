"""Observe the actual remote main before recording a repository-only release."""
from pathlib import Path

from .delivery import _context_repo, _git, guard_ref, released, resolve_ref
from .evidence import write_artifact
from .state import HiveError, load_task, state_directory


def observe(task_id: str, repo: Path, ref: str, *, state_dir: Path | None = None) -> dict:
    expected = resolve_ref(repo, ref)
    gated = guard_ref(repo, ref, phase="release", state_dir=state_dir)
    if task_id not in gated["task_ids"]:
        raise HiveError("repository observation task is not guarded")
    remote = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()
    if len(remote) != 2 or remote != [expected, "refs/heads/main"]:
        raise HiveError("actual remote main differs from repository release SHA")
    directory = state_directory(state_dir)
    task = load_task(task_id, state_dir=directory)
    artifact = write_artifact(directory, "repository", {"schema_version": 1, "observer": "repository",
        "observed_sha": expected, "remote_sha": remote[0], "remote_ref": remote[1],
        "repo": _context_repo(task, repo)})
    return released(task_id, repo, expected, expected, artifact=artifact, component="repository", state_dir=directory)
