"""Pheromone field: decay, queen need, DAG law, dispatch settle, lease honesty."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from hive import dispatch
from hive.scent import DEFAULT_HALF_LIFE, deposit, field, intensity, map_field, on_fail, on_finish, on_queen_plan
from hive.state import HiveError, create_task, locked_task, mark_node, validate_task, _read, _save
from hive.workplan import normalize_plan, select_ready
from hive.workplan_runtime import claim, heartbeat, install, recover


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

    def test_unverified_trace_does_not_break_the_dag(self):
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
        self.assertGreater(sum(mark["intensity"] for mark in live if mark["kind"] == "unverified"), 0.5)
        ready = select_ready(plan, scent_field=live)
        self.assertNotIn("soldier-verify", ready["ready"])
        self.assertIn("dependency not succeeded: worker-docs", ready["blocked"]["soldier-verify"])

    def test_attraction_orders_ready_candidates(self):
        packages = [
            _pkg("worker-high", writes=["high/out"]),
            _pkg("worker-low", writes=["low/out"]),
            _pkg("worker-alarm", writes=["alarm/out"]),
        ]
        plan = {"schema_version": 1, "max_parallel": 3, "packages": packages}
        task = {"scent": {"schema_version": 1, "half_life_seconds": 300, "marks": []}}
        deposit(task, path="high/out", kind="need", by="queen", amount=0.9)
        deposit(task, path="low/out", kind="need", by="queen", amount=0.2)
        deposit(task, path="alarm/out", kind="need", by="queen", amount=1.0)
        deposit(task, path="alarm/out", kind="alarm", by="worker", amount=1.0)
        ready = select_ready(plan, scent_field=field(task))
        self.assertEqual(ready["ready"], ["worker-high", "worker-low", "worker-alarm"])

    def test_alarm_diffuses_to_parent_paths(self):
        task = {}
        on_fail(task, _pkg("worker-api", writes=["src/api.py"]), by="worker")
        kinds = {(m["path"], m["kind"]) for m in field(task)}
        self.assertIn(("src/api.py", "alarm"), kinds)
        self.assertIn(("src", "alarm"), kinds)

    def test_finished_worker_recruits_queued_siblings(self):
        packages = [
            _pkg("worker-src", writes=["src"]),
            _pkg("worker-docs", writes=["docs"]),
        ]
        task = {"workplan": {"packages": packages}}
        finished = dict(packages[0])
        finished["status"] = "succeeded"
        on_finish(task, finished, by="worker")
        docs_need = [m for m in field(task) if m["path"] == "docs" and m["kind"] == "need"]
        self.assertTrue(docs_need)
        self.assertGreaterEqual(docs_need[0]["intensity"], 0.3)
        self.assertEqual(docs_need[0]["package_id"], "worker-src")

    def test_map_groups_marks_by_path(self):
        task = {}
        deposit(task, path="src", kind="need", by="queen", amount=1.0)
        deposit(task, path="src", kind="alarm", by="worker", amount=0.5)
        rows = {row["path"]: row for row in map_field(task)}
        self.assertIn("src", rows)
        self.assertGreater(rows["src"]["need"], 0.5)
        self.assertGreater(rows["src"]["alarm"], 0.2)

    def test_without_scent_soldier_waits_for_dependencies(self):
        packages = [
            _pkg("worker-src", writes=["src"]),
            _pkg("soldier-verify", deps=["worker-src"], reads=["src"], writes=[".hive-verify"]),
        ]
        plan = {"schema_version": 1, "max_parallel": 2, "packages": packages}
        ready = select_ready(plan)
        self.assertEqual(ready["ready"], ["worker-src"])
        self.assertIn("soldier-verify", ready["blocked"])

    def test_deposit_prunes_decayed_marks(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        task = {}
        deposit(task, path="old/src", kind="need", by="queen", now=now)
        later = now + timedelta(seconds=DEFAULT_HALF_LIFE * 10)
        deposit(task, path="new/src", kind="need", by="queen", now=later)
        self.assertEqual([mark["path"] for mark in task["scent"]["marks"]], ["new/src"])
        self.assertEqual({mark["path"] for mark in field(task, now=later)}, {"new/src"})

    def test_deposit_rejects_naive_now(self):
        task = {}
        with self.assertRaises(HiveError):
            deposit(task, path="src", kind="need", by="queen", now=datetime(2026, 1, 1))
        self.assertNotIn("scent", task)

    def test_deposit_naive_now_with_existing_marks_stays_a_domain_error(self):
        task = {}
        deposit(task, path="old/src", kind="need", by="queen", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        with self.assertRaises(HiveError):
            deposit(task, path="new/src", kind="need", by="queen", now=datetime(2026, 1, 1, 0, 0, 1))


class _InstalledTask(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.task = create_task("scent integration", size="S", source_key=self._testMethodName, state_dir=self.state)
        for node in ("intake", "plan"):
            mark_node(self.task["id"], node, "succeeded", owner="p", artifact="x", state_dir=self.state)
        install(self.task["id"], {"max_parallel": 1, "packages": [{
            "id": "worker-src", "title": "worker-src", "depends_on": [],
            "read_paths": ["hive/state.py"], "write_paths": ["hive/dispatch.py"],
            "acceptance": ["unit"], "priority": 1, "max_attempts": 2}]}, owner="p", state_dir=self.state)

    def tearDown(self):
        self.temp.cleanup()

    def _marks(self):
        task = _read(self.task["id"], self.state)
        return {(mark["path"], mark["kind"]) for mark in field(task)}

    def _busy(self):
        task = _read(self.task["id"], self.state)
        return [mark for mark in task["scent"]["marks"] if mark["kind"] == "busy"]


class DispatchScentTests(_InstalledTask):
    def setUp(self):
        super().setUp()
        self.artifact = Path(self.temp.name) / "artifact"
        self.artifact.write_text("ok", encoding="utf-8")

    def _settle(self, *, success):
        entry = dispatch.enqueue(self.task["id"], "worker-src", owner="worker", state_dir=self.state)
        entry = dispatch.reserve(self.task["id"], entry["id"], owner="worker", token=entry["token"],
                                 launch={"repo": "x"}, state_dir=self.state)
        entry = dispatch.acknowledge(self.task["id"], entry["id"], owner="worker", token=entry["token"],
                                     session_id="session", state_dir=self.state)
        dispatch.settle(self.task["id"], entry["id"], owner="worker", token=entry["token"], success=success,
                        artifact=str(self.artifact), reason="" if success else "bad", state_dir=self.state)

    def test_dispatch_success_leaves_done_and_unverified(self):
        self._settle(success=True)
        marks = self._marks()
        self.assertNotIn(("hive/dispatch.py", "busy"), marks)
        self.assertIn(("hive/dispatch.py", "done"), marks)
        self.assertIn(("hive/dispatch.py", "unverified"), marks)

    def test_dispatch_failure_leaves_alarm(self):
        self._settle(success=False)
        marks = self._marks()
        self.assertNotIn(("hive/dispatch.py", "busy"), marks)
        self.assertIn(("hive/dispatch.py", "alarm"), marks)


class LeaseScentTests(_InstalledTask):
    def test_heartbeat_refreshes_busy_trace(self):
        package = claim(self.task["id"], "worker-src", owner="worker", state_dir=self.state)
        aged = (datetime.now(timezone.utc) - timedelta(seconds=250)).isoformat()
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory)
            for mark in task["scent"]["marks"]:
                if mark["kind"] == "busy":
                    mark["at"] = aged
            _save(task, directory)
        stale = self._busy()
        self.assertEqual(len(stale), 1)
        self.assertLess(intensity(stale[0]), 0.6)
        heartbeat(self.task["id"], "worker-src", owner="worker", token=package["lease"]["token"], state_dir=self.state)
        fresh = self._busy()
        self.assertEqual(len(fresh), 1)
        self.assertGreater(intensity(fresh[0]), 0.9)

    def test_recover_evaporates_dead_holder_busy_trace(self):
        claim(self.task["id"], "worker-src", owner="worker", state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory)
            task["workplan"]["packages"][0]["lease"]["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            _save(task, directory)
        recover(self.task["id"], owner="reaper", reason="expired", state_dir=self.state)
        self.assertEqual(self._busy(), [])


class ScentValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.task = create_task("validate scent", size="S", state_dir=self.state)

    def tearDown(self):
        self.temp.cleanup()

    def test_corrupt_scent_is_rejected_on_save(self):
        corrupt = _read(self.task["id"], self.state)
        corrupt["scent"] = {"schema_version": 9}
        with self.assertRaises(HiveError):
            validate_task(corrupt)
        with locked_task(self.task["id"], self.state) as directory:
            loaded = _read(self.task["id"], directory)
            loaded["scent"] = {"schema_version": 9}
            with self.assertRaises(HiveError):
                _save(loaded, directory)


if __name__ == "__main__":
    unittest.main()
