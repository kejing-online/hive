"""Focused persistent workplan lease tests.

Run: python -m unittest scripts.tests.test_hive_workplan_runtime
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hive.state import HiveError, create_task, locked_task, mark_node, _read, _save
from hive.workplan_runtime import _artifact, claim, fail, finish, heartbeat, install, recover, retry, status


class WorkplanRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.artifact = Path(self.temp.name) / "artifact.txt"
        self.artifact.write_text("package output", encoding="utf-8")
        self.task = create_task("runtime test", size="S", source_key="runtime-test", state_dir=self.state)
        mark_node(self.task["id"], "intake", "succeeded", owner="planner", artifact="intake", state_dir=self.state)
        mark_node(self.task["id"], "plan", "succeeded", owner="planner", artifact="plan", state_dir=self.state)

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, *, attempts=2):
        return {"max_parallel": 2, "packages": [
            {"id": "one", "title": "one", "depends_on": [], "read_paths": ["scripts/hive/state.py"],
             "write_paths": ["scripts/hive/workplan_runtime.py"], "acceptance": ["unit"], "priority": 10,
             "max_attempts": attempts},
            {"id": "two", "title": "two", "depends_on": ["one"], "read_paths": ["scripts/hive/workplan_runtime.py"],
             "write_paths": ["scripts/tests/test_hive_workplan_runtime.py"], "acceptance": ["unit"], "priority": 1,
             "max_attempts": 2},
        ]}

    def test_competing_claim_has_one_winner_and_finish_artifact(self):
        install(self.task["id"], self.spec(), owner="planner", state_dir=self.state)
        def contender(owner):
            try:
                return claim(self.task["id"], "one", owner=owner, state_dir=self.state)
            except HiveError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, ("a", "b")))
        first = next(result for result in results if result is not None)
        self.assertEqual(sum(result is not None for result in results), 1)
        with self.assertRaises(HiveError):
            claim(self.task["id"], "two", owner="b", state_dir=self.state)
        with self.assertRaises(HiveError):
            finish(self.task["id"], "one", owner=first["lease"]["owner"], token=first["lease"]["token"], artifact="missing", state_dir=self.state)
        done = finish(self.task["id"], "one", owner=first["lease"]["owner"], token=first["lease"]["token"], artifact=str(self.artifact), state_dir=self.state)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(len(done["artifact"]["sha256"]), 64)
        self.assertEqual(status(self.task["id"], state_dir=self.state)["ready"], ["two"])

    def test_expiry_recovery_retry_and_attempt_cap(self):
        install(self.task["id"], self.spec(attempts=1), owner="planner", state_dir=self.state)
        package = claim(self.task["id"], "one", owner="a", state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory)
            task["workplan"]["packages"][0]["lease"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            _save(task, directory)
        revision = _read(self.task["id"], self.state)["revision"]
        with self.assertRaises(HiveError):
            heartbeat(self.task["id"], "one", owner="a", token=package["lease"]["token"], state_dir=self.state)
        with self.assertRaises(HiveError):
            finish(self.task["id"], "one", owner="a", token=package["lease"]["token"], artifact=str(self.artifact), state_dir=self.state)
        self.assertEqual(_read(self.task["id"], self.state)["revision"], revision)
        recover(self.task["id"], owner="recovery", reason="expired", state_dir=self.state)
        with self.assertRaises(HiveError):
            retry(self.task["id"], "one", owner="a", reason="retry", state_dir=self.state)

    def test_failed_package_can_retry_with_remaining_budget(self):
        install(self.task["id"], self.spec(attempts=2), owner="planner", state_dir=self.state)
        package = claim(self.task["id"], "one", owner="a", state_dir=self.state)
        fail(self.task["id"], "one", owner="a", token=package["lease"]["token"], reason="broken", state_dir=self.state)
        retried = retry(self.task["id"], "one", owner="b", reason="fixed", state_dir=self.state)
        self.assertEqual(retried["status"], "queued")
        again = claim(self.task["id"], "one", owner="b", state_dir=self.state)
        with self.assertRaises(HiveError):
            finish(self.task["id"], "one", owner="b", token=package["lease"]["token"], artifact=str(self.artifact), state_dir=self.state)
        self.assertEqual(again["attempts"], 2)
        fail(self.task["id"], "one", owner="b", token=again["lease"]["token"], reason="again", state_dir=self.state)
        with self.assertRaises(HiveError):
            retry(self.task["id"], "one", owner="b", reason="no budget", state_dir=self.state)

    def test_frozen_task_status_has_no_ready_work_and_claim_is_fenced(self):
        install(self.task["id"], self.spec(), owner="planner", state_dir=self.state)
        with locked_task(self.task["id"], self.state) as directory:
            task = _read(self.task["id"], directory)
            task["frozen"] = True
            _save(task, directory)
        projection = status(self.task["id"], state_dir=self.state)
        self.assertEqual(projection["ready"], [])
        self.assertEqual(projection["blocked"]["one"], ["task is frozen"])
        with self.assertRaises(HiveError):
            claim(self.task["id"], "one", owner="worker", state_dir=self.state)

    def test_artifact_io_and_non_ascii_token_are_domain_errors(self):
        with patch("hive.workplan_runtime.Path.resolve", side_effect=RuntimeError("loop")):
            with self.assertRaises(HiveError):
                _artifact(str(self.artifact))
        install(self.task["id"], self.spec(), owner="planner", state_dir=self.state)
        package = claim(self.task["id"], "one", owner="a", state_dir=self.state)
        with self.assertRaises(HiveError):
            heartbeat(self.task["id"], "other", owner="a", token=package["lease"]["token"], state_dir=self.state)
        with self.assertRaises(HiveError):
            heartbeat(self.task["id"], "one", owner="other", token=package["lease"]["token"], state_dir=self.state)
        with self.assertRaises(HiveError):
            heartbeat(self.task["id"], "one", owner="a", token="wrong", state_dir=self.state)
        with self.assertRaises(HiveError):
            heartbeat(self.task["id"], "one", owner="a", token="é", state_dir=self.state)
        self.assertEqual(package["status"], "running")

    def test_install_replay_preserves_live_package_state_and_rejects_new_spec(self):
        spec = self.spec()
        install(self.task["id"], spec, owner="planner", state_dir=self.state)
        claimed = claim(self.task["id"], "one", owner="worker", state_dir=self.state)
        before = _read(self.task["id"], self.state)
        replay = install(self.task["id"], spec, owner="other", state_dir=self.state)
        after = _read(self.task["id"], self.state)
        self.assertEqual(replay["packages"][0]["lease"], claimed["lease"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["workplan"], before["workplan"])
        changed = self.spec()
        changed["packages"][0]["priority"] = 99
        with self.assertRaises(HiveError):
            install(self.task["id"], changed, owner="other", state_dir=self.state)


if __name__ == "__main__":
    unittest.main()
