"""Run bounded Codex work packages through the existing Hive control plane.

The durable spawn intent is written before Popen. An ambiguous start is never
automatically repeated; a completed result can be reconciled without rerunning
the agent. This coordinates cooperating local processes, not an OS sandbox.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import dispatch, workspaces, execution_adapters
from .state import HiveError, load_task, state_directory
from .workplan_runtime import heartbeat

SOURCE = Path(__file__).resolve().parents[2]
MAX_LOG_BYTES = 16 * 1024 * 1024
OUTPUT_SCHEMA = {
    "type": "object", "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
        "remaining": {"type": "array", "items": {"type": "string"}},
    }, "required": ["status", "summary", "remaining"], "additionalProperties": False,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: dict):
    if path.is_symlink():
        raise HiveError("execution artifact must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read(path: Path):
    try:
        if path.is_symlink():
            raise HiveError("execution artifact must not be a symlink")
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HiveError(f"unreadable execution artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise HiveError("execution artifact must be an object")
    return value


def _digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory(task_id, dispatch_id, directory):
    # get validates identifiers before they become a filesystem path.
    dispatch.get(task_id, dispatch_id, state_dir=directory)
    result = directory / "executions" / dispatch_id
    if result.is_symlink() or result.resolve() != directory.resolve() / "executions" / dispatch_id:
        raise HiveError("execution directory escapes configured state")
    return result


def _identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {"pid": pid, "start_ticks": stat[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return None


def _alive(identity):
    return isinstance(identity, dict) and type(identity.get("pid")) is int and _identity(identity["pid"]) == identity


def _package(task, entry):
    return next(p for p in task["workplan"]["packages"] if p["id"] == entry["package_id"])


def _timeout(value):
    if type(value) is not int or not 1 <= value <= 3600:
        raise HiveError("timeout_seconds must be an integer from 1 through 3600")
    return value


def _model(value):
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 100 or value.startswith("-")):
        raise HiveError("invalid model name")
    return value


def _prompt(task, package):
    return (
        "You are the implementation worker for one already-authorized Hive work package. "
        "The coordinator owns integration and deployment. Do not spawn additional agents, "
        "create a new Hive identity, commit, push, merge, deploy, or modify task state. "
        "You share a repository with other workers; preserve their changes. "
        "Work only inside this assigned Git worktree and the declared write paths. "
        "Do not read credentials or environment files. Run only necessary targeted validation; "
        "never run full-site checks. If blocked, preserve work and report what remains. "
        "Finish with the requested JSON report; completed means this package's acceptance is met.\n"
        + json.dumps({"hive_task_id": task["id"], "goal": task["goal"],
                      "package": {key: package[key] for key in
                                  ("id", "title", "read_paths", "write_paths", "acceptance")}}, ensure_ascii=False)
    )


def launch(task_id, dispatch_id, *, repo: Path, base_ref="HEAD", model=None,
           timeout_seconds=600, resume_checkpoint: Path | None = None, adapter=None,
           command_file=None, state_dir=None):
    """Start a pending outbox entry once, returning before the agent finishes."""
    directory = state_directory(state_dir)
    entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if entry["status"] != "pending":
        raise HiveError("only pending dispatch can be launched; reconcile an existing execution")
    _timeout(timeout_seconds); _model(model)
    requested = entry.get("requested_adapter")
    adapter_spec = execution_adapters.select(adapter if adapter is not None else requested, command_file=command_file, which=shutil.which)
    if adapter_spec["name"] == "external":
        raise HiveError("external dispatches are created by handoff, not launch")
    if requested is not None and requested != adapter_spec["name"]:
        raise HiveError("pending dispatch adapter does not match requested_adapter")
    task = load_task(task_id, state_dir=directory)
    from .delivery import _context_repo
    _context_repo(task, Path(repo))
    workspace_id = hashlib.sha256((task_id + ":" + entry["package_id"]).encode()).hexdigest()
    workspace = workspaces.prepare(Path(repo), base_ref, workspace_id=workspace_id, root=directory / "workspaces")
    package = _package(task, entry)
    observed = workspaces.inspect(workspace, package["write_paths"])
    if resume_checkpoint is not None:
        workspaces.assert_checkpoint(workspace, Path(resume_checkpoint))
    elif not observed["clean"]:
        raise HiveError("existing work requires an explicit verified resume checkpoint")
    if observed["violations"]:
        raise HiveError("workspace has out-of-scope changes")
    execution_dir = _directory(task_id, dispatch_id, directory)
    execution_dir.mkdir(parents=True, exist_ok=True)
    launch_spec = {"repo": str(Path(repo).resolve()), "base_sha": workspace["base_sha"],
                   "worktree": workspace["worktree"], "workspace": workspace,
                   "execution_dir": str(execution_dir), "model": model, "timeout_seconds": timeout_seconds,
                   "adapter": adapter_spec["name"], "adapter_spec": {key: value for key, value in adapter_spec.items() if key not in {"parser", "stdin_prompt"}},
                   "command_file": str(command_file) if command_file is not None else None,
                   "reserved_at": _now()}
    dispatch.reserve(task_id, dispatch_id, owner=entry["owner"], token=entry["token"],
                     launch=launch_spec, state_dir=directory)
    # No automatic repeat if this process dies between reserve and Popen.
    with (execution_dir / "supervisor.log").open("ab") as output:
        child = subprocess.Popen([sys.executable, "-m", "hive.executor", "supervise", task_id, dispatch_id,
                                  "--state-dir", str(directory)], cwd=SOURCE, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    identity = _identity(child.pid)
    if identity:
        _write(execution_dir / "process.json", {"supervisor": identity, "at": _now(), "pid": child.pid})
    return {"dispatch_id": dispatch_id, "supervisor_pid": child.pid, "worktree": workspace["worktree"], "status": "starting"}


def run(task_id, package_id, *, repo, owner, base_ref="HEAD", model=None,
        timeout_seconds=600, resume_checkpoint=None, adapter=None, command_file=None, state_dir=None):
    _timeout(timeout_seconds); _model(model)
    # Resolve configuration before enqueue: bad adapters must not mutate outbox.
    adapter_spec = execution_adapters.select(adapter, command_file=command_file, which=shutil.which)
    if adapter_spec["name"] == "external":
        raise HiveError("external dispatches are created by handoff, not run")
    entry = dispatch.enqueue(task_id, package_id, owner=owner, ttl_seconds=900, adapter=adapter_spec["name"], state_dir=state_dir)
    return launch(task_id, entry["id"], repo=Path(repo), base_ref=base_ref, model=model,
                  timeout_seconds=timeout_seconds, resume_checkpoint=resume_checkpoint, adapter=adapter,
                  command_file=command_file, state_dir=state_dir)


def _events(path):
    if path.stat().st_size > MAX_LOG_BYTES:
        raise HiveError("agent event log exceeded execution limit")
    return execution_adapters.parse_events(path)


def _stop(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if child.poll() is None:
        child.wait(timeout=5)


def supervise(task_id, dispatch_id, *, state_dir=None):
    directory = state_directory(state_dir)
    entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    execution_dir = _directory(task_id, dispatch_id, directory)
    execution_dir.mkdir(parents=True, exist_ok=True)
    with (execution_dir / "supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HiveError("execution supervisor is already active")
        if (execution_dir / "result.json").exists():
            reconcile(task_id, dispatch_id, state_dir=directory)
            return 0
        if entry["status"] != "starting":
            raise HiveError("execution is not reserved for startup")
        # Durable at-most-once startup barrier. Unknown attempts need inspection.
        try:
            with (execution_dir / "spawn-intent.json").open("x") as stream:
                json.dump({"dispatch_id": dispatch_id, "at": _now()}, stream)
                stream.flush(); os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise HiveError("spawn intent already exists; reconcile without restarting") from exc
        _write(execution_dir / "process.json", {"supervisor": _identity(os.getpid()), "at": _now()})
        spec = entry["launch"]
        task = load_task(task_id, state_dir=directory)
        package = _package(task, entry)
        child = None
        cancelled = False
        def on_signal(_signum, _frame):
            nonlocal cancelled
            cancelled = True
        old_handler = signal.signal(signal.SIGTERM, on_signal)
        reason, code = "", 1
        summary = {"status": "blocked", "summary": "Execution did not finish", "remaining": ["inspect execution logs"]}
        events = {"session_id": None, "completed": False, "failed": False, "usage": {}}
        try:
            if (execution_dir / "cancel.json").exists():
                raise HiveError("execution cancelled before agent startup")
            heartbeat(task_id, entry["package_id"], owner=entry["owner"], token=entry["token"], state_dir=directory)
            _write(execution_dir / "output-schema.json", OUTPUT_SCHEMA)
            prompt = _prompt(task, package)
            prompt_file = execution_dir / "prompt.txt"; prompt_file.write_text(prompt)
            adapter_name = spec.get("adapter", "codex")
            adapter_spec = (execution_adapters.restore(spec["adapter_spec"])
                            if spec.get("adapter_spec") else execution_adapters.select(adapter_name, command_file=spec.get("command_file"), which=shutil.which))
            command = execution_adapters.build(adapter_spec, worktree=str(spec["worktree"]), prompt_file=prompt_file,
                                               output_file=execution_dir / "agent-output.json", schema_file=execution_dir / "output-schema.json", model=spec["model"])
            from . import isolation
            if adapter_name in {"command", "grok", "codex"} and isolation.write_restriction_available():
                roots = isolation.provider_write_roots(adapter_name, Path(spec["worktree"]), execution_dir)
                command = isolation.wrap_argv(
                    command,
                    roots,
                    mode="command" if adapter_name == "command" else "provider",
                )
            _write(execution_dir / "launch.json", {"adapter": adapter_name, "argv": command, "at": _now()})
            env = isolation.worker_env(adapter_name, {
                "HIVE_TASK_ID": task_id,
                "HIVE_STATE_DIR": str(directory),
                "HIVE_WORKTREE": str(spec["worktree"]),
                "HIVE_PROMPT_FILE": str(prompt_file),
                "HIVE_OUTPUT_FILE": str(execution_dir / "agent-output.json"),
                "HIVE_MODEL": spec["model"] or "",
            })
            if adapter_name in {"command", "grok", "codex"}:
                env.update(isolation.scratch_env(execution_dir))
            with (execution_dir / "events.jsonl").open("wb") as output, (execution_dir / "stderr.log").open("wb") as error:
                child = subprocess.Popen(command, cwd=spec["worktree"], env=env, stdin=subprocess.PIPE if adapter_spec["stdin_prompt"] else subprocess.DEVNULL,
                                         stdout=output, stderr=error, start_new_session=True, close_fds=True)
                _write(execution_dir / "process.json", {"supervisor": _identity(os.getpid()),
                                                        "child": _identity(child.pid), "at": _now()})
                if adapter_spec["stdin_prompt"]:
                    child.stdin.write(prompt.encode()); child.stdin.close()
                deadline = time.monotonic() + _timeout(spec["timeout_seconds"])
                while True:
                    if cancelled or (execution_dir / "cancel.json").exists():
                        reason = "execution cancelled"; _stop(child); code = 143; break
                    if time.monotonic() >= deadline:
                        reason = "execution timed out"; _stop(child); code = 124; break
                    try:
                        code = child.wait(timeout=min(5, max(.01, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        code = None
                    events_path = execution_dir / "events.jsonl"
                    if events_path.exists() and events_path.stat().st_size > MAX_LOG_BYTES:
                        raise HiveError("agent event log exceeded execution limit")
                    events = adapter_spec["parser"](events_path)
                    if (execution_dir / "stderr.log").stat().st_size > MAX_LOG_BYTES:
                        raise HiveError("agent stderr exceeded execution limit")
                    if events["session_id"]:
                        dispatch.acknowledge(task_id, dispatch_id, owner=entry["owner"], token=entry["token"],
                                             session_id=events["session_id"], state_dir=directory)
                    heartbeat(task_id, entry["package_id"], owner=entry["owner"], token=entry["token"], state_dir=directory)
                    if code is not None:
                        break
                grok_report = execution_adapters.report_from_output(adapter_name, execution_dir / "events.jsonl")
                if grok_report is not None:
                    _write(execution_dir / "agent-output.json", grok_report)
                if (execution_dir / "agent-output.json").exists():
                    summary = _read(execution_dir / "agent-output.json")
        except (HiveError, OSError, ValueError, BrokenPipeError) as exc:
            reason = str(exc)
            code = 1
        finally:
            if child is not None:
                _stop(child)
            signal.signal(signal.SIGTERM, old_handler)
        remaining = summary.get("remaining")
        if not isinstance(remaining, list) or any(not isinstance(item, str) for item in remaining):
            remaining = ["invalid agent report"]
        observed = workspaces.inspect(spec["workspace"], package["write_paths"])
        success = bool(code == 0 and events["session_id"] and events["completed"] and not events["failed"]
                       and summary.get("status") == "completed" and isinstance(summary.get("summary"), str) and not remaining and not observed["violations"] and not reason)
        if not success and not reason:
            reason = "agent did not meet execution/report/scope requirements"
        checkpoint_path = execution_dir / "checkpoint.json"
        workspaces.checkpoint(spec["workspace"], package["write_paths"], remaining=remaining or ([] if success else [reason]), output=checkpoint_path)
        result = {"dispatch_id": dispatch_id, "task_id": task_id, "attempt": entry["attempt"],
                  "session_id": events["session_id"], "success": success, "exit_code": code,
                  "reason": reason, "summary": summary, "usage": events["usage"], "at": _now(),
                  "checkpoint": str(checkpoint_path), "checkpoint_sha256": _digest(checkpoint_path),
                  "scope": observed, "events_sha256": _digest(execution_dir / "events.jsonl") if (execution_dir / "events.jsonl").exists() else None}
        _write(execution_dir / "result.json", result)
        reconcile(task_id, dispatch_id, state_dir=directory)
        return 0 if success else 1


def reconcile(task_id, dispatch_id, *, state_dir=None):
    directory = state_directory(state_dir)
    entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if entry.get("launch", {}).get("adapter") == "external":
        from . import handoff
        return handoff.reconcile(task_id, dispatch_id, state_dir=directory)
    execution_dir = _directory(task_id, dispatch_id, directory)
    result_path = execution_dir / "result.json"
    if result_path.exists():
        result = _read(result_path)
        if (result.get("dispatch_id") != dispatch_id or result.get("task_id") != task_id
                or result.get("attempt") != entry["attempt"] or type(result.get("success")) is not bool):
            raise HiveError("execution result identity does not match outbox")
        checkpoint_path = execution_dir / "checkpoint.json"
        if result.get("checkpoint") != str(checkpoint_path) or result.get("checkpoint_sha256") != _digest(checkpoint_path):
            raise HiveError("execution checkpoint digest mismatch")
        # Completed historical records remain readable after a later attempt edits the tree.
        if result["success"] and entry["status"] not in {"succeeded", "failed"}:
            workspaces.assert_checkpoint(entry["launch"]["workspace"], checkpoint_path)
        if result["success"]:
            if result.get("exit_code") != 0 or not result.get("session_id") or result.get("scope", {}).get("violations"):
                raise HiveError("successful result lacks execution/scope evidence")
            if entry["status"] not in {"succeeded", "failed"}:
                dispatch.acknowledge(task_id, dispatch_id, owner=entry["owner"], token=entry["token"],
                                     session_id=result["session_id"], terminal_replay=True, state_dir=directory)
        return dispatch.settle(task_id, dispatch_id, owner=entry["owner"], token=entry["token"],
                               success=result["success"], artifact=str(result_path), reason=result.get("reason", ""), state_dir=directory)
    if entry["status"] in {"pending", "succeeded", "failed", "uncertain"}:
        return entry
    process_path = execution_dir / "process.json"
    if process_path.exists() and _alive(_read(process_path).get("supervisor")):
        return entry
    if entry["status"] == "starting":
        try:
            reserved = datetime.fromisoformat(entry["launch"]["reserved_at"])
            if 0 <= (datetime.now(timezone.utc) - reserved).total_seconds() < 15:
                return entry
        except (KeyError, TypeError, ValueError):
            pass
    # Reserve/Popen/process-ack is not one OS transaction. Never guess it did not start.
    return dispatch.mark_uncertain(task_id, dispatch_id, owner=entry["owner"], token=entry["token"],
                                   reason="no live supervisor or terminal result; inspect before retry", state_dir=directory)


def cancel(task_id, dispatch_id, *, owner, reason, state_dir=None):
    directory = state_directory(state_dir)
    entry = dispatch.get(task_id, dispatch_id, state_dir=directory)
    if owner != entry["owner"] or not isinstance(reason, str) or not reason.strip():
        raise HiveError("cancellation requires the dispatch owner and a reason")
    if entry.get("launch", {}).get("adapter") == "external":
        from . import handoff
        return handoff.cancel(task_id, dispatch_id, owner=owner, reason=reason, state_dir=directory)
    if entry["status"] in {"succeeded", "failed"}:
        return entry
    execution_dir = _directory(task_id, dispatch_id, directory)
    _write(execution_dir / "cancel.json", {"at": _now(), "reason": reason, "owner": owner})
    process_path = execution_dir / "process.json"
    if process_path.exists():
        identity = _read(process_path).get("supervisor")
        if _alive(identity):
            os.kill(identity["pid"], signal.SIGTERM)
    return {"dispatch_id": dispatch_id, "status": "cancellation_requested"}


def drive(task_id, *, repo, owner, model=None, adapter=None, command_file=None, max_seconds=600, max_packages=3, capacity=2, state_dir=None):
    """Bounded coordinator window; existing attempts are adopted, never relaunched."""
    from .workplan_runtime import status
    _timeout(max_seconds)
    if type(max_packages) is not int or not 1 <= max_packages <= 32:
        raise HiveError("max_packages must be an integer from 1 through 32")
    if type(capacity) is not int or not 1 <= capacity <= 32:
        raise HiveError("capacity must be an integer from 1 through 32")
    directory = state_directory(state_dir)
    deadline = time.monotonic() + max_seconds
    launched = 0
    while time.monotonic() < deadline:
        entries = dispatch.list_entries(task_id, state_dir=directory)
        active = sum(entry["status"] in {"starting", "running", "uncertain"} for entry in entries)
        for entry in entries:
            if entry["status"] == "pending" and launched < max_packages and active < capacity:
                selected = entry.get("requested_adapter") or adapter or os.environ.get("HIVE_EXECUTOR_ADAPTER") or "codex"
                if selected == "external":
                    continue
                launch(task_id, entry["id"], repo=repo, model=model, adapter=selected, command_file=command_file, state_dir=directory)
                launched += 1
                active += 1
            elif entry["status"] in {"starting", "running"}:
                reconcile(task_id, entry["id"], state_dir=directory)
        projection = status(task_id, capacity=capacity, state_dir=directory)
        for package_id in projection["ready"][:max(0, max_packages-launched)]:
            run(task_id, package_id, repo=repo, owner=owner, model=model, adapter=adapter, command_file=command_file, state_dir=directory)
            launched += 1
        current = dispatch.list_entries(task_id, state_dir=directory)
        live = {"pending", "starting", "running", "uncertain"}
        if not any(entry["status"] in live for entry in current):
            return {"task_id": task_id, "launched": launched, "dispatches": current}
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    return {"task_id": task_id, "launched": launched, "status": "window_elapsed",
            "dispatches": dispatch.list_entries(task_id, state_dir=directory)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("supervise",))
    parser.add_argument("task_id"); parser.add_argument("dispatch_id")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    try:
        return supervise(args.task_id, args.dispatch_id, state_dir=Path(args.state_dir) if args.state_dir else None)
    except (HiveError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)})); return 2


if __name__ == "__main__":
    raise SystemExit(main())
