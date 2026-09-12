"""Human-readable Hive status. Does not advance the DAG."""
from __future__ import annotations

from .state import DONE, load_task


def inspect_task(task_id: str, *, state_dir=None) -> dict:
    task = load_task(task_id, state_dir=state_dir)
    nodes = task.get("nodes") or []
    waiting = [n for n in nodes if n.get("status") not in DONE]
    current = waiting[0] if waiting else None
    holders = [n.get("owner") for n in nodes if n.get("owner") and n.get("status") not in DONE]
    if current is None:
        reason = "所有节点已完成工作流；发布回执仍可能未齐"
    else:
        deps = current.get("depends_on") or []
        pending_deps = [d for d in deps if any(n["id"] == d and n.get("status") not in DONE for n in nodes)]
        if pending_deps:
            reason = f"停在 {current['id']}，因为依赖未完成：{','.join(pending_deps)}"
        else:
            reason = f"停在 {current['id']}（{current.get('status')}），下一步由 {current.get('owner') or '尚未指派的工牌'} 处理"
    return {
        "task_id": task["id"],
        "goal": task.get("goal"),
        "size": task.get("size"),
        "kind": task.get("kind"),
        "hive_status": task.get("status"),
        "next_node": None if current is None else current["id"],
        "holders": holders,
        "human": reason,
        "nodes": [{"id": n["id"], "status": n.get("status"), "owner": n.get("owner")} for n in nodes],
    }
