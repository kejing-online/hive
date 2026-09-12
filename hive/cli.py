"""Hive Intake CLI. Usage: python -m hive.cli doctor"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .delivery import admit_landed_main, guard_ref, integrated, register, released
from .guard import validate
from .state import (
    HiveError,
    create_task,
    load_task,
    mark_node,
    retry_node,
    state_directory,
)


def _dir(value: str) -> Path:
    return state_directory(Path(value) if value else None)


def _execution_parser(sub):
    command = sub.add_parser("execute", help="Run or hand off work packages using a selected executor")
    actions = command.add_subparsers(dest="action", required=True)
    actions.add_parser("adapters", help="List local executor adapters and capabilities")
    for action in ("run", "launch", "status", "reconcile", "cancel", "drive", "handoff", "attach", "heartbeat", "submit"):
        child = actions.add_parser(action)
        child.add_argument("task_id")
        child.add_argument("--state-dir", default="")
        if action in {"run", "handoff"}:
            child.add_argument("package_id")
        if action in {"launch", "reconcile", "cancel", "attach", "heartbeat", "submit"}:
            child.add_argument("dispatch_id")
        if action in {"run", "drive", "cancel", "handoff", "attach", "heartbeat", "submit"}:
            child.add_argument("--owner", required=True)
        if action in {"run", "launch", "drive", "handoff"}:
            child.add_argument("--repo", required=True)
        if action in {"run", "launch", "drive"}:
            child.add_argument("--model")
            child.add_argument("--adapter", help="Executor adapter; explicit selection never falls back")
            child.add_argument("--command-file", help="Explicit argv JSON for the command adapter")
        if action in {"run", "launch", "handoff"}:
            child.add_argument("--base-ref", default="HEAD")
            child.add_argument("--resume-checkpoint")
        if action in {"run", "launch"}:
            child.add_argument("--timeout-seconds", type=int, default=600)
        if action == "drive":
            child.add_argument("--max-seconds", type=int, default=600)
            child.add_argument("--max-packages", type=int, default=3)
            child.add_argument("--capacity", type=int, default=2)
        if action == "cancel":
            child.add_argument("--reason", required=True)
        if action == "handoff":
            child.add_argument("--executor", required=True, help="External tool identity, for example grok-editor")
        if action in {"attach", "heartbeat", "submit"}:
            child.add_argument("--token", required=True)
        if action == "attach":
            child.add_argument("--session-id", required=True)
        if action == "submit":
            child.add_argument("--report", required=True, help="External result JSON; never executed as commands")


def _execution_action(args):
    from . import executor, dispatch
    if args.action == "adapters":
        from .execution_adapters import describe
        return describe()
    options = {"state_dir": _dir(args.state_dir)}
    if args.action in {"handoff", "attach", "heartbeat", "submit"}:
        from . import handoff
        options["owner"] = args.owner
        if args.action == "handoff":
            return handoff.prepare(args.task_id, args.package_id, repo=Path(args.repo),
                                   executor_name=args.executor, base_ref=args.base_ref,
                                   resume_checkpoint=Path(args.resume_checkpoint) if args.resume_checkpoint else None,
                                   **options)
        options["token"] = args.token
        if args.action == "attach":
            options["session_id"] = args.session_id
        elif args.action == "submit":
            options["report"] = Path(args.report)
        return getattr(handoff, args.action)(args.task_id, args.dispatch_id, **options)
    if args.action == "status":
        return dispatch.list_entries(args.task_id, **options)
    if args.action in {"reconcile", "cancel"}:
        if args.action == "cancel":
            options.update(owner=args.owner, reason=args.reason)
        return getattr(executor, args.action)(args.task_id, args.dispatch_id, **options)
    options.update(repo=Path(args.repo), model=args.model, adapter=args.adapter,
                   command_file=Path(args.command_file) if args.command_file else None)
    if args.action == "drive":
        return executor.drive(args.task_id, owner=args.owner, max_seconds=args.max_seconds,
                              max_packages=args.max_packages, capacity=args.capacity, **options)
    options.update(base_ref=args.base_ref, timeout_seconds=args.timeout_seconds,
                   resume_checkpoint=Path(args.resume_checkpoint) if args.resume_checkpoint else None)
    if args.action == "run":
        return executor.run(args.task_id, args.package_id, owner=args.owner, **options)
    return executor.launch(args.task_id, args.dispatch_id, **options)


def _workplan_parser(sub) -> None:
    command = sub.add_parser("workplan", help="Plan, claim and recover work within one Hive task")
    actions = command.add_subparsers(dest="action", required=True)
    for action in ("install", "status", "claim", "heartbeat", "finish", "fail", "recover", "retry"):
        child = actions.add_parser(action)
        child.add_argument("task_id")
        child.add_argument("--state-dir", default="")
        if action != "status":
            child.add_argument("--owner", required=True)
        if action == "install":
            child.add_argument("spec", help="JSON plan file; commands in acceptance are never executed")
        if action == "status":
            child.add_argument("--capacity", type=int)
        if action in {"claim", "heartbeat", "finish", "fail", "retry"}:
            child.add_argument("package_id")
        if action in {"claim", "heartbeat"}:
            child.add_argument("--ttl-seconds", type=int, default=900)
        if action in {"heartbeat", "finish", "fail"}:
            child.add_argument("--token", required=True)
        if action == "finish":
            child.add_argument("--artifact", required=True)
        if action in {"fail", "recover", "retry"}:
            child.add_argument("--reason", required=True)


def _workplan_action(args):
    from . import workplan_runtime as runtime
    options = {"state_dir": _dir(args.state_dir)}
    if args.action == "status":
        return runtime.status(args.task_id, capacity=args.capacity, **options)
    options["owner"] = args.owner
    if args.action == "install":
        try:
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HiveError("cannot read workplan JSON: " + str(exc)) from exc
        return runtime.install(args.task_id, spec, **options)
    if args.action == "recover":
        return runtime.recover(args.task_id, reason=args.reason, **options)
    if args.action in {"claim", "heartbeat"}:
        options["ttl_seconds"] = args.ttl_seconds
    if args.action in {"heartbeat", "finish", "fail"}:
        options["token"] = args.token
    if args.action == "finish":
        options["artifact"] = args.artifact
    if args.action in {"fail", "retry"}:
        options["reason"] = args.reason
    return getattr(runtime, args.action)(args.task_id, args.package_id, **options)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _execution_parser(sub)
    _workplan_parser(sub)
    intake = sub.add_parser("intake"); intake.add_argument("goal"); intake.add_argument("--size", default="M"); intake.add_argument("--state-dir", default="")
    intake.add_argument("--source-key", default="")
    intake.add_argument("--kind", choices=("development", "analysis"), default="development")
    status = sub.add_parser("status"); status.add_argument("task_id"); status.add_argument("--state-dir", default="")
    node = sub.add_parser("node"); node.add_argument("task_id"); node.add_argument("node_id"); node.add_argument("status"); node.add_argument("--owner", default=""); node.add_argument("--artifact", default=""); node.add_argument("--approved", action="store_true"); node.add_argument("--state-dir", default="")
    guard = sub.add_parser("guard"); guard.add_argument("task_id"); guard.add_argument("--phase", choices=("integrate", "release", "complete"), default="integrate"); guard.add_argument("--state-dir", default="")
    retry = sub.add_parser("retry")
    retry.add_argument("task_id")
    retry.add_argument("node_id")
    retry.add_argument("--owner", required=True)
    retry.add_argument("--reason", required=True)
    retry.add_argument("--state-dir", default="")
    registered = sub.add_parser("register")
    registered.add_argument("task_id")
    registered.add_argument("repo_path")
    registered.add_argument("ref")
    registered.add_argument("--state-dir", default="")
    registered.add_argument("--rework-reason", default="")
    registered.add_argument("--base-ref", default="")
    ref_guard = sub.add_parser("guard-ref")
    ref_guard.add_argument("repo_path")
    ref_guard.add_argument("ref")
    ref_guard.add_argument("--phase", choices=("integrate", "release"), default="integrate")
    ref_guard.add_argument("--state-dir", default="")
    integrated_cmd = sub.add_parser("integrated")
    integrated_cmd.add_argument("task_id")
    integrated_cmd.add_argument("repo_path")
    integrated_cmd.add_argument("main_ref")
    integrated_cmd.add_argument("--artifact", required=True)
    integrated_cmd.add_argument("--state-dir", default="")
    released_cmd = sub.add_parser("released")
    released_cmd.add_argument("task_id")
    released_cmd.add_argument("repo_path")
    released_cmd.add_argument("expected_ref")
    released_cmd.add_argument("observed_ref")
    released_cmd.add_argument("--artifact", required=True)
    released_cmd.add_argument("--component", action="append", required=True)
    released_cmd.add_argument("--state-dir", default="")
    capture = sub.add_parser("capture-source")
    capture.add_argument("task_id"); capture.add_argument("repo_path"); capture.add_argument("ref", nargs="?", default="HEAD")
    capture.add_argument("--path", action="append"); capture.add_argument("--worktree", action="store_true")
    capture.add_argument("--base-ref", default="")
    capture.add_argument("--state-dir", default="")
    admit = sub.add_parser("admit-main")
    admit.add_argument("repo_path")
    admit.add_argument("--ref", default="refs/remotes/origin/main")
    admit.add_argument("--artifact", default="")
    admit.add_argument("--state-dir", default="")
    evidence = sub.add_parser("bind-evidence")
    evidence.add_argument("task_id"); evidence.add_argument("phase", choices=("verify", "review", "security"))
    evidence.add_argument("snapshot"); evidence.add_argument("report"); evidence.add_argument("--owner", required=True)
    evidence.add_argument("--state-dir", default=""); evidence.add_argument("--additional-snapshot", action="append", default=[])
    repository = sub.add_parser("observe-repository")
    repository.add_argument("task_id"); repository.add_argument("repo_path"); repository.add_argument("ref")
    repository.add_argument("--state-dir", default="")
    snapshot_cmd = sub.add_parser("snapshot")
    snapshot_cmd.add_argument("--home", default=""); snapshot_cmd.add_argument("--max-age-seconds", type=float, default=900)
    swarm_cmd = sub.add_parser("swarm", help="Intake, install a plan, drive workers; never releases")
    swarm_cmd.add_argument("goal")
    swarm_cmd.add_argument("--repo", required=True)
    swarm_cmd.add_argument("--adapter", default="")
    swarm_cmd.add_argument("--command-file", default="")
    swarm_cmd.add_argument("--plan", default="", help="Optional workplan JSON; default is exactly one package")
    swarm_cmd.add_argument("--write", action="append", default=[], help="Default-package write path (repeatable)")
    swarm_cmd.add_argument("--size", default="M")
    swarm_cmd.add_argument("--source-key", default="")
    swarm_cmd.add_argument("--owner", default="swarm")
    swarm_cmd.add_argument("--max-seconds", type=int, default=600)
    swarm_cmd.add_argument("--max-packages", type=int, default=16)
    swarm_cmd.add_argument("--capacity", type=int, default=4)
    swarm_cmd.add_argument("--state-dir", default="")
    roster_cmd = sub.add_parser("roster", help="Who is running on a task")
    roster_cmd.add_argument("task_id")
    roster_cmd.add_argument("--state-dir", default="")
    sub.add_parser("doctor")
    inspect_cmd = sub.add_parser("inspect")
    inspect_cmd.add_argument("task_id")
    inspect_cmd.add_argument("--state-dir", default="")
    mcp_cmd = sub.add_parser("mcp")
    mcp_cmd.add_argument("--serve", action="store_true", help="stdio MCP; argv tools only")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "execute":
            result = _execution_action(args)
        elif args.cmd == "workplan":
            result = _workplan_action(args)
        elif args.cmd == "swarm":
            from .swarm import load_plan_file, swarm
            plan = load_plan_file(Path(args.plan)) if args.plan else None
            result = swarm(
                args.goal, repo=Path(args.repo),
                adapter=args.adapter or None,
                command_file=Path(args.command_file) if args.command_file else None,
                plan=plan, size=args.size, source_key=args.source_key,
                owner=args.owner, max_seconds=args.max_seconds,
                max_packages=args.max_packages, capacity=args.capacity,
                write_paths=args.write or None,
                state_dir=_dir(args.state_dir),
            )
        elif args.cmd == "roster":
            from .swarm import roster
            result = roster(args.task_id, state_dir=_dir(args.state_dir))
        elif args.cmd == "doctor":
            from .doctor import report
            result = report()
        elif args.cmd == "inspect":
            from .inspect import inspect_task
            result = inspect_task(args.task_id, state_dir=_dir(args.state_dir))
        elif args.cmd == "mcp":
            from .mcp_stdio import serve
            return serve()
        elif args.cmd == "snapshot":
            from .status_snapshot import snapshot
            result = snapshot(home=Path(args.home) if args.home else None, max_age_seconds=args.max_age_seconds)
        elif args.cmd == "capture-source":
            from .evidence import capture_source
            result = {"snapshot": capture_source(args.task_id, Path(args.repo_path), args.ref, worktree=args.worktree, paths=args.path, base_ref=args.base_ref, state_dir=_dir(args.state_dir))}
        elif args.cmd == "bind-evidence":
            from .evidence import record_evidence
            result = record_evidence(args.task_id, args.phase, [args.snapshot, *args.additional_snapshot], Path(args.report), owner=args.owner, state_dir=_dir(args.state_dir))
        elif args.cmd == "observe-repository":
            from .repository_receipts import observe
            result = observe(args.task_id, Path(args.repo_path), args.ref, state_dir=_dir(args.state_dir))
        elif args.cmd == "intake": result = create_task(args.goal, size=args.size, kind=args.kind, state_dir=_dir(args.state_dir), source_key=args.source_key)
        elif args.cmd == "status": result = load_task(args.task_id, state_dir=_dir(args.state_dir))
        elif args.cmd == "node": result = mark_node(args.task_id, args.node_id, args.status, owner=args.owner, artifact=args.artifact, approved=args.approved, state_dir=_dir(args.state_dir))
        elif args.cmd == "retry": result = retry_node(args.task_id, args.node_id, owner=args.owner, reason=args.reason, state_dir=_dir(args.state_dir))
        elif args.cmd == "register": result = register(args.task_id, Path(args.repo_path), args.ref, state_dir=_dir(args.state_dir), rework_reason=args.rework_reason, base_ref=args.base_ref)
        elif args.cmd == "guard-ref": result = guard_ref(Path(args.repo_path), args.ref, phase=args.phase, state_dir=_dir(args.state_dir))
        elif args.cmd == "admit-main": result = admit_landed_main(Path(args.repo_path), args.ref, artifact=args.artifact, state_dir=_dir(args.state_dir))
        elif args.cmd == "integrated": result = integrated(args.task_id, Path(args.repo_path), args.main_ref, artifact=args.artifact, state_dir=_dir(args.state_dir))
        elif args.cmd == "released": result = released(args.task_id, Path(args.repo_path), args.expected_ref, args.observed_ref, artifact=args.artifact, component=args.component, state_dir=_dir(args.state_dir))
        else: result = validate(load_task(args.task_id, state_dir=_dir(args.state_dir)), phase=args.phase, state_dir=_dir(args.state_dir))
    except (HiveError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)); return 2
    print(json.dumps({"ok": True, "task": result}, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
