"""Pheromone field: decay, queen need, soldiers follow unverified traces."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from hive.scent import DEFAULT_HALF_LIFE, deposit, field, on_finish, on_queen_plan
from hive.workplan import normalize_plan, select_ready


def _pkg(ident, *, deps=(), reads=(), writes=(), priority=50):
    return {
        "id": ident, "title": ident, "depends_on": list(deps),
        "read_paths": list(reads), "write_paths": list(writes),
        "acceptance": ["ok"], "priority": priority, "max_attempts": 2,
        "status": "queued", "attempts": 0, "lease": None,
    }


class ScentTests(unittest.TestCase):
    def test_marks_evaporate(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        task = {}
        deposit(task, path="src", kind="need", by="queen", amount=1.0, now=now)
        later = now + timedelta(seconds=DEFAULT_HALF_LIFE * 10)
        self.assertEqual(field(task, now=later), [])
        soon = now + timedelta(seconds=1)
        live = field(task, now=soon)
        self.assertEqual(len(live), 1)
        self.assertGreater(live[0]["intensity"], 0.5)

    def test_queen_lays_need_on_worker_slices(self):
        task = {}
        plan = normalize_plan({
            "max_parallel": 2,
            "packages": [
                {k: v for k, v in _pkg("worker-src", writes=["src"]).items()
                 if k not in {"status", "attempts", "lease"}},
                {k: v for k, v in _pkg("soldier-verify", deps=["worker-src"], reads=["src"], writes=[".hive-verify"]).items()
                 if k not in {"status", "attempts", "lease"}},
            ],
        })
        on_queen_plan(task, plan["packages"], by="swarm")
        kinds = {(m["path"], m["kind"]) for m in field(task)}
        self.assertIn(("src", "need"), kinds)
        self.assertNotIn((".hive-verify", "need"), kinds)

    def test_soldier_follows_unverified_without_waiting_every_edge(self):
        packages = [
            _pkg("worker-src", writes=["src"]),
            _pkg("worker-docs", writes=["docs"]),
            _pkg("soldier-verify", deps=["worker-src", "worker-docs"], reads=["src", "docs"], writes=[".hive-verify"]),
        ]
        plan = {"schema_version": 1, "max_parallel": 4, "packages": packages}
        task = {"scent": {"schema_version": 1, "half_life_seconds": 300, "marks": []}}
        worker = dict(packages[0])
        worker["status"] = "succeeded"
        on_finish(task, worker, by="worker")
        live = field(task)
        ready = select_ready(plan, scent_field=live)
        self.assertIn("soldier-verify", ready["ready"])
        self.assertNotIn("soldier-verify", ready["blocked"])

    def test_without_scent_soldier_waits_for_dependencies(self):
        packages = [
            _pkg("worker-src", writes=["src"]),
            _pkg("soldier-verify", deps=["worker-src"], reads=["src"], writes=[".hive-verify"]),
        ]
        plan = {"schema_version": 1, "max_parallel": 2, "packages": packages}
        ready = select_ready(plan)
        self.assertEqual(ready["ready"], ["worker-src"])
        self.assertIn("soldier-verify", ready["blocked"])
