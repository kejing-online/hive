"""Provider command construction and evidence parsing for Hive executions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from .state import HiveError


def _empty():
    return {"session_id": None, "completed": False, "failed": False, "usage": {}}


def _usage(value):
    return value if isinstance(value, dict) else {}


def _event_evidence(event, result):
    """Accept only structured provider events; never infer success from text."""
    typ = event.get("type")
    if typ in {"thread.started", "session.started"}:
        identity = event.get("thread_id") or event.get("session_id")
        if isinstance(identity, str) and identity:
            result["session_id"] = identity
    elif typ in {"turn.completed", "execution.completed"}:
        result["completed"] = True
        result["usage"] = _usage(event.get("usage"))
    elif typ in {"turn.failed", "execution.failed", "error"}:
        result["failed"] = True
        result["usage"] = _usage(event.get("usage")) or result["usage"]


def parse_codex_events(path: Path):
    return parse_events(path)


def parse_command_events(path: Path):
    return parse_events(path)


def parse_grok_events(path: Path):
    """Parse the Grok 1.0.24 JSON object captured by the protocol probe."""
    result = _empty()
    if not path.exists():
        return result
    try:
        value = json.loads(path.read_text(errors="replace"))
    except ValueError:
        return result
    if not isinstance(value, dict):
        return result
    identity = value.get("sessionId")
    report = value.get("structuredOutput")
    if isinstance(identity, str) and identity:
        result["session_id"] = identity
    result["usage"] = _usage(value.get("usage"))
    if value.get("stopReason") == "end_turn" and isinstance(report, dict):
        if report.get("status") == "completed":
            result["completed"] = True
        elif report.get("status") == "blocked":
            result["failed"] = True
    return result


def parse_events(path: Path):
    result = _empty()
    if not path.exists():
        return result
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            _event_evidence(event, result)
    return result


def report_from_output(name: str, events_path: Path):
    """Return a provider's structured final report, if its protocol carries one."""
    if name != "grok" or not events_path.exists():
        return None
    try:
        value = json.loads(events_path.read_text(errors="replace"))
    except ValueError:
        return None
    report = value.get("structuredOutput") if isinstance(value, dict) else None
    return report if isinstance(report, dict) else None


def _command_file(value):
    if value is None:
        raise HiveError("command adapter requires command_file")
    path = Path(value)
    try:
        argv = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HiveError("command_file must contain a JSON argv list") from exc
    if (not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
            or argv[0].startswith("-")):
        raise HiveError("command_file must contain a non-empty JSON argv list")
    return argv


def select(name=None, *, command_file=None, which=None):
    which = which or shutil.which
    selected = name or os.environ.get("HIVE_EXECUTOR_ADAPTER") or "codex"
    if not isinstance(selected, str) or selected not in {"codex", "grok", "command", "external"}:
        raise HiveError("unknown execution adapter")
    if selected == "external":
        return {"name": selected, "argv": None, "stdin_prompt": False, "parser": None}
    if selected == "command":
        argv = _command_file(command_file)
        binary = which(argv[0]) if "/" not in argv[0] else argv[0]
        if not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise HiveError("command adapter executable is unavailable")
        argv[0] = str(Path(binary).resolve())
        return {"name": selected, "argv": argv, "stdin_prompt": False, "parser": parse_command_events}
    binary_name = "codex" if selected == "codex" else "grok"
    binary = which(binary_name)
    if not binary:
        raise HiveError(f"{binary_name} executable is unavailable")
    return {"name": selected, "binary": binary, "stdin_prompt": selected == "codex",
            "parser": parse_codex_events if selected == "codex" else parse_grok_events}


def restore(saved):
    """Restore an immutable launch specification without consulting user config."""
    if not isinstance(saved, dict) or saved.get("name") not in {"codex", "grok", "command"}:
        raise HiveError("invalid persisted execution adapter specification")
    name = saved["name"]
    if name == "command":
        argv = saved.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
            raise HiveError("invalid persisted command argv")
        return {"name": name, "argv": list(argv), "stdin_prompt": False, "parser": parse_command_events}
    binary = saved.get("binary")
    if not isinstance(binary, str) or not binary:
        raise HiveError("invalid persisted provider executable")
    return {"name": name, "binary": binary, "stdin_prompt": name == "codex",
            "parser": parse_codex_events if name == "codex" else parse_grok_events}


def describe(which=None):
    which = which or shutil.which
    return {
        "codex": {"available": bool(which("codex")), "events": "codex_jsonl", "report": "output_last_message"},
        "grok": {"available": bool(which("grok")), "events": "grok_json", "report": "structuredOutput"},
        "command": {"available": True, "events": "normalized_jsonl", "report": "HIVE_OUTPUT_FILE", "requires": "command_file"},
        "external": {"available": True, "attested": True, "process": False},
    }


def build(spec, *, worktree: str, prompt_file: Path, output_file: Path, schema_file: Path, model):
    name = spec["name"]
    if name == "command":
        return list(spec["argv"])
    if name == "codex":
        command = [spec["binary"], "exec", "--json", "-C", worktree, "-s", "danger-full-access",
                   "-c", 'approval_policy="never"', "-c", "agents.enabled=false", "--output-schema",
                   str(schema_file), "--output-last-message", str(output_file), "-"]
        if model is not None:
            command[2:2] = ["--model", model]
        return command
    command = [spec["binary"], "--prompt-file", str(prompt_file), "--cwd", worktree,
               "--always-approve", "--no-subagents", "--max-turns", "20", "--output-format", "json", "--json-schema", schema_file.read_text()]
    if model is not None:
        command += ["--model", model]
    return command
