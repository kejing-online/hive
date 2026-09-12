"""Minimal command-adapter worker. Writes assigned paths, then a structured report."""
from __future__ import annotations

import json
import os
from pathlib import Path

root = Path(os.environ["HIVE_WORKTREE"])
prompt = Path(os.environ["HIVE_PROMPT_FILE"]).read_text(encoding="utf-8")
spec = json.loads(prompt[prompt.find("{") :])
for rel in spec["package"]["write_paths"]:
    dest = root / rel
    if dest.suffix:
        dest.parent.mkdir(parents=True, exist_ok=True)
        previous = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        dest.write_text(previous + ("\n" if previous else "") + "# hive-worker\n", encoding="utf-8")
    else:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".hive-touched").write_text("ok\n", encoding="utf-8")
Path(os.environ["HIVE_OUTPUT_FILE"]).write_text(
    json.dumps({"status": "completed", "summary": "stamped assigned paths", "remaining": []}),
    encoding="utf-8",
)
print(json.dumps({"type": "session.started", "session_id": "example-worker"}), flush=True)
print(json.dumps({"type": "execution.completed", "usage": {"output_tokens": 1}}), flush=True)
