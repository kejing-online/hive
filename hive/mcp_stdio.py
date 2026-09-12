"""Stdio MCP for Hive. Argv arrays only. Not a permission boundary."""
from __future__ import annotations

import json
import sys

from .doctor import report as doctor_report
from .inspect import inspect_task
from .state import load_task, state_directory

WHITELIST = ("hive_status", "hive_doctor", "hive_inspect")


def _tools() -> list[dict]:
    return [
        {"name": "hive_doctor", "description": "Honest capability probe",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "hive_status", "description": "Load a Hive task",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "hive_inspect", "description": "Why this task is stopped",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    ]


def call_tool(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name not in WHITELIST:
        return {"ok": False, "error": "UNKNOWN_TOOL", "code": "UNKNOWN_TOOL"}
    if name == "hive_doctor":
        return doctor_report()
    if name == "hive_status":
        return load_task(str(arguments["task_id"]), state_dir=state_directory())
    if name == "hive_inspect":
        return inspect_task(str(arguments["task_id"]), state_dir=state_directory())
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
            "serverInfo": {"name": "hive", "version": "0.3.0"},
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
            continue
        reply = handle(message)
        if reply is None:
            continue
        outgoing.write(json.dumps(reply, ensure_ascii=False) + "\n")
        outgoing.flush()
    return 0
