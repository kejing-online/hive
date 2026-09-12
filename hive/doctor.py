"""Honest capability probe. Never reports a missing adapter as live."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import isolation

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_TOOLS = (
    "hive_classify", "hive_start", "hive_record", "hive_status",
    "hive_doctor", "hive_snapshot", "hive_inspect", "hive_roster", "hive_scent",
)


def report() -> dict:
    grok = shutil.which("grok")
    codex = shutil.which("codex")
    return {
        "ok": True,
        "mode": "hive-dag",
        "python": sys.version.split()[0],
        "hive_dag": (REPO_ROOT / "hive" / "state.py").is_file(),
        "factory": (REPO_ROOT / "hive" / "factory.py").is_file(),
        "swarm": (REPO_ROOT / "hive" / "swarm.py").is_file(),
        "record_gates": True,
        "grok_connected": bool(grok),
        "grok_path": grok,
        "codex_connected": bool(codex),
        "os_sandbox": False,
        "isolation": isolation.probe(),
        "tenant_runtime": False,
        "demo_is_not_live": True,
        "mcp_tools": list(MCP_TOOLS),
        "forbidden_tools": ["ads_server", "deploy_to_prod", "shell", "composio"],
        "unavailable": [name for name, flag in (("grok", bool(grok)), ("codex", bool(codex))) if not flag],
    }
