"""Size a goal. Does not start a task and does not invent stages."""
from __future__ import annotations


_SMALL = ("typo", "hotfix", "one-line", "one line", "rename", "copy fix")
_LARGE = ("rewrite", "migrate", "architecture", "across module", "strategic")


def classify_goal(goal: str) -> dict:
    text = str(goal or "")
    lower = text.lower()
    size = "M"
    if any(token in lower for token in _SMALL) and not any(token in lower for token in _LARGE):
        size = "S"
    if any(token in lower for token in _LARGE) or lower.startswith("l:") or lower.startswith("l："):
        size = "L"
    return {
        "ok": True,
        "size": size,
        "goal": text,
        "starts_task": False,
    }
