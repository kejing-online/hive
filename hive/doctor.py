"""Honest capability probe. Never reports a missing adapter as live."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import isolation

REPO_ROOT = Path(__file__).resolve().parents[1]


def report() -> dict:
    grok = shutil.which("grok")
    return {
        "ok": True,
        "mode": "hive-dag",
        "python": sys.version.split()[0],
        "hive_dag": (REPO_ROOT / "hive" / "state.py").is_file(),
        "record_gates": True,
        "grok_connected": bool(grok),
        "grok_path": grok,
        "os_sandbox": False,
        "isolation": isolation.probe(),
        "tenant_runtime": False,
        "demo_is_not_live": True,
        "mcp_tools": ["hive_status", "hive_doctor", "hive_inspect"],
        "forbidden_tools": ["ads_server", "deploy_to_prod", "shell", "composio"],
        "unavailable": [name for name, flag in (("grok", bool(grok)),) if not flag],
    }
