"""Stdio MCP for Hive. Argv arrays only. Not a permission boundary."""
from __future__ import annotations

import json
import sys

from .classify import classify_goal
from .doctor import report as doctor_report
from .inspect import inspect_task
from .state import HiveError, create_task, load_task, mark_node, state_directory
from .status_snapshot import snapshot
from .scent import field as scent_field
from .swarm import roster

WHITELIST = (
    "hive_classify",
    "hive_start",
    "hive_record",
    "hive_status",
    "hive_doctor",
    "hive_snapshot",
    "hive_inspect",
    "hive_roster",
    "hive_scent",
)

_VERDICT = {
    "PASS": "succeeded",
    "APPROVE": "succeeded",
    "succeeded": "succeeded",
    "FAIL": "failed",
    "REQUEST_CHANGES": "failed",
    "failed": "failed",
    "SKIP": "skipped",
    "skipped": "skipped",
}


def _tools() -> list[dict]:
    return [
        {"name": "hive_classify", "description": "Size a goal; does not start a task",
         "inputSchema": {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}},
        {"name": "hive_start", "description": "Create a Hive task",
         "inputSchema": {"type": "object", "properties": {
             "goal": {"type": "string"}, "size": {"type": "string"}, "source_key": {"type": "string"},
         }, "required": ["goal"]}},
        {"name": "hive_record", "description": "Record a node verdict",
         "inputSchema": {"type": "object", "properties": {
             "task_id": {"type": "string"}, "phase": {"type": "string"},
             "verdict": {"type": "string"}, "agent": {"type": "string"}, "notes": {"type": "string"},
         }, "required": ["task_id", "phase", "verdict", "agent"]}},
        {"name": "hive_status", "description": "Load a Hive task",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "hive_doctor", "description": "Honest capability probe",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "hive_snapshot", "description": "Read-only hive projection",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "hive_inspect", "description": "Why this task is stopped",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "hive_roster", "description": "Queen/worker/soldier roster",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "hive_scent", "description": "Decaying path marks",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    ]


def call_tool(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name not in WHITELIST:
        return {"ok": False, "error": "UNKNOWN_TOOL", "code": "UNKNOWN_TOOL"}
    try:
        if name == "hive_doctor":
            return doctor_report()
        if name == "hive_classify":
            return classify_goal(str(arguments.get("goal") or ""))
        if name == "hive_start":
            size = str(arguments.get("size") or "M").upper()
            if size not in {"S", "M", "L"}:
                size = "M"
            task = create_task(
                str(arguments["goal"]),
                size=size,
                source_key=str(arguments.get("source_key") or ""),
            )
            return {"ok": True, "task_id": task["id"], "size": task["size"]}
        if name == "hive_record":
            status = _VERDICT.get(str(arguments["verdict"]))
            if not status:
                raise HiveError("unknown verdict")
            task = mark_node(
                str(arguments["task_id"]),
                str(arguments["phase"]),
                status,
                owner=str(arguments["agent"]),
                artifact=str(arguments.get("notes") or "mcp-record"),
            )
            return {"ok": True, "task_id": task["id"], "status": task["status"]}
        if name == "hive_status":
            return load_task(str(arguments["task_id"]), state_dir=state_directory())
        if name == "hive_snapshot":
            return snapshot()
        if name == "hive_inspect":
            return inspect_task(str(arguments["task_id"]), state_dir=state_directory())
        if name == "hive_roster":
            return roster(str(arguments["task_id"]))
        if name == "hive_scent":
            task = load_task(str(arguments["task_id"]), state_dir=state_directory())
            return {"task_id": task["id"], "marks": scent_field(task)}
    except (HiveError, KeyError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "UNKNOWN_TOOL"}


def handle(message: dict) -> dict | None:
    if "id" not in message and message.get("method") in {"notifications/initialized", "initialized"}:
        return None
    req_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hive", "version": "0.4.0"},
        }
    elif method == "tools/list":
        result = {"tools": _tools()}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = {"content": [{"type": "text", "text": json.dumps(
            call_tool(params.get("name", ""), params.get("arguments") or {}), ensure_ascii=False)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": method or "missing method"}}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve(stdin=None, stdout=None) -> int:
    incoming = stdin or sys.stdin
    outgoing = stdout or sys.stdout
    for line in incoming:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            outgoing.write(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}) + "\n")
            outgoing.flush()
            continue
        reply = handle(message)
        if reply is not None:
            outgoing.write(json.dumps(reply, ensure_ascii=False) + "\n")
            outgoing.flush()
    return 0
